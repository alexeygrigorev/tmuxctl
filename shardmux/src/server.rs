use std::collections::VecDeque;
use std::fs;
use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use portable_pty::{
    ChildKiller, CommandBuilder, ExitStatus, PtySize, PtySystem, native_pty_system,
};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use uuid::Uuid;

use crate::model::{SessionRecord, SessionState};
use crate::protocol::{decode_resize, kind, read_frame, write_frame};
use crate::registry::Registry;
use crate::util::set_mode;

const IO_CHUNK_BYTES: usize = 8192;
const FRAME_CHUNK_BYTES: usize = 64 * 1024;

pub fn run(id: Uuid) -> Result<()> {
    let registry = Registry::open()?;
    match run_inner(&registry, id) {
        Ok(()) => Ok(()),
        Err(error) => {
            let message = format!("session server failed: {error:#}");
            let current = registry.load(id).ok();
            if let Some(record) = current {
                let _ = registry.update(id, |stored| {
                    stored.state = SessionState::Failed;
                    stored.server_pid = None;
                    stored.child_pid = None;
                    stored.message = Some(message.clone());
                    Ok(())
                });
                let _ = registry.release_name_if_matches(&record.name, id);
                let _ = fs::remove_file(&record.socket);
            }
            Err(error)
        }
    }
}

fn run_inner(registry: &Registry, id: Uuid) -> Result<()> {
    let record = registry.load(id)?;
    if record.socket.exists() {
        fs::remove_file(&record.socket).with_context(|| {
            format!("failed to remove stale socket {}", record.socket.display())
        })?;
    }

    let listener = UnixListener::bind(&record.socket)
        .with_context(|| format!("failed to bind socket {}", record.socket.display()))?;
    set_mode(&record.socket, 0o600)
        .with_context(|| format!("failed to protect socket {}", record.socket.display()))?;
    listener.set_nonblocking(true)?;

    let pty_system = native_pty_system();
    let pair = pty_system.openpty(PtySize {
        rows: 24,
        cols: 80,
        pixel_width: 0,
        pixel_height: 0,
    })?;
    let command = build_command(&record)?;
    let mut child = pair
        .slave
        .spawn_command(command)
        .context("failed to spawn session command in a PTY")?;
    let child_pid = child.process_id();
    let mut killer = child.clone_killer();
    drop(pair.slave);

    let reader = pair.master.try_clone_reader()?;
    let writer = pair.master.take_writer()?;
    let shared = Arc::new(Shared::new(writer, record.scrollback_bytes));

    registry.update(id, |stored| {
        stored.state = SessionState::Running;
        stored.server_pid = Some(std::process::id());
        stored.child_pid = child_pid;
        stored.message = None;
        Ok(())
    })?;

    let terminate = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(SIGTERM, Arc::clone(&terminate))?;
    signal_hook::flag::register(SIGINT, Arc::clone(&terminate))?;

    let (command_tx, command_rx) = mpsc::channel();
    let stop_listener = Arc::new(AtomicBool::new(false));
    let listener_handle = spawn_listener(
        listener,
        Arc::clone(&shared),
        command_tx,
        Arc::clone(&stop_listener),
    );

    let (reader_done_tx, reader_done_rx) = mpsc::channel();
    let output_shared = Arc::clone(&shared);
    thread::spawn(move || {
        let result = copy_pty_output(reader, output_shared);
        let _ = reader_done_tx.send(result);
    });

    let (exit_tx, exit_rx) = mpsc::channel();
    thread::spawn(move || {
        let result = child.wait();
        let _ = exit_tx.send(result);
    });

    let status = supervise(
        &*pair.master,
        &mut *killer,
        &command_rx,
        &exit_rx,
        &terminate,
    )?;

    let _ = reader_done_rx.recv_timeout(Duration::from_millis(250));
    let exit_message = if let Some(signal) = status.signal() {
        format!("session terminated by {signal}")
    } else {
        format!("session exited with code {}", status.exit_code())
    };
    shared.broadcast(kind::EXIT, exit_message.as_bytes());
    shared.disconnect_active();

    stop_listener.store(true, Ordering::Relaxed);
    let _ = listener_handle.join();

    let latest = registry.load(id)?;
    registry.update(id, |stored| {
        stored.state = if status.success() {
            SessionState::Exited
        } else {
            SessionState::Failed
        };
        stored.server_pid = None;
        stored.child_pid = None;
        stored.exit_signal = status.signal().map(ToOwned::to_owned);
        stored.exit_code = if status.signal().is_none() {
            Some(status.exit_code())
        } else {
            None
        };
        stored.message = Some(exit_message.clone());
        Ok(())
    })?;
    registry.release_name_if_matches(&latest.name, id)?;
    let _ = fs::remove_file(&latest.socket);
    Ok(())
}

fn build_command(record: &SessionRecord) -> Result<CommandBuilder> {
    let mut command = if record.command.is_empty() {
        let shell = std::env::var_os("SHELL").unwrap_or_else(|| "/bin/sh".into());
        let mut command = CommandBuilder::new(shell);
        command.arg("-l");
        command
    } else {
        let program = record
            .command
            .first()
            .ok_or_else(|| anyhow!("empty command"))?;
        let mut command = CommandBuilder::new(program);
        command.args(record.command.iter().skip(1));
        command
    };
    command.cwd(&record.cwd);
    command.env("TERM", "xterm-256color");
    command.env("SHARDMUX_SESSION", &record.name);
    command.env("SHARDMUX_SESSION_ID", record.id.to_string());
    Ok(command)
}

fn supervise(
    master: &dyn portable_pty::MasterPty,
    killer: &mut dyn ChildKiller,
    commands: &Receiver<ServerCommand>,
    exits: &Receiver<io::Result<ExitStatus>>,
    terminate: &AtomicBool,
) -> Result<ExitStatus> {
    let mut termination_sent = false;
    loop {
        if terminate.load(Ordering::Relaxed) && !termination_sent {
            let _ = killer.kill();
            termination_sent = true;
        }

        loop {
            match commands.try_recv() {
                Ok(ServerCommand::Resize { cols, rows }) => {
                    master.resize(PtySize {
                        rows,
                        cols,
                        pixel_width: 0,
                        pixel_height: 0,
                    })?;
                }
                Ok(ServerCommand::Kill) => {
                    let _ = killer.kill();
                    termination_sent = true;
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }

        match exits.recv_timeout(Duration::from_millis(100)) {
            Ok(result) => return result.context("failed while waiting for session command"),
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                bail!("child wait thread stopped without reporting an exit status")
            }
        }
    }
}

fn spawn_listener(
    listener: UnixListener,
    shared: Arc<Shared>,
    commands: Sender<ServerCommand>,
    stop: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while !stop.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((stream, _address)) => {
                    let shared = Arc::clone(&shared);
                    let commands = commands.clone();
                    thread::spawn(move || {
                        if let Err(error) = handle_client(stream, shared, commands) {
                            eprintln!("client connection failed: {error:#}");
                        }
                    });
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(25));
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(error) => {
                    eprintln!("socket accept failed: {error}");
                    break;
                }
            }
        }
    })
}

fn handle_client(
    mut stream: UnixStream,
    shared: Arc<Shared>,
    commands: Sender<ServerCommand>,
) -> Result<()> {
    let mut active_id = None;
    loop {
        let Some(frame) = read_frame(&mut stream)? else {
            break;
        };
        match frame.kind {
            kind::ATTACH => {
                if active_id.is_some() {
                    send_response(&mut stream, kind::ERROR, b"client is already attached")?;
                    continue;
                }
                let id = shared.attach(&stream)?;
                active_id = Some(id);
            }
            kind::INPUT => {
                require_active(&shared, active_id)?;
                shared.write_input(&frame.payload)?;
            }
            kind::RESIZE => {
                require_active(&shared, active_id)?;
                let (cols, rows) = decode_resize(&frame.payload)?;
                commands.send(ServerCommand::Resize { cols, rows })?;
            }
            kind::DETACH => {
                if let Some(id) = active_id.take() {
                    shared.detach(id);
                }
                break;
            }
            kind::PING => send_response(&mut stream, kind::PONG, b"")?,
            kind::INJECT => {
                shared.write_input(&frame.payload)?;
                send_response(&mut stream, kind::ACK, b"")?;
            }
            kind::KILL => {
                send_response(&mut stream, kind::ACK, b"")?;
                commands.send(ServerCommand::Kill)?;
                break;
            }
            other => {
                send_response(
                    &mut stream,
                    kind::ERROR,
                    format!("unknown frame type {other}").as_bytes(),
                )?;
            }
        }
    }
    if let Some(id) = active_id {
        shared.detach(id);
    }
    Ok(())
}

fn require_active(shared: &Shared, active_id: Option<u64>) -> Result<()> {
    let id = active_id.context("client must attach before sending terminal input")?;
    if !shared.is_active(id) {
        bail!("this attachment was superseded by another client");
    }
    Ok(())
}

fn send_response(stream: &mut UnixStream, kind: u8, payload: &[u8]) -> Result<()> {
    write_frame(stream, kind, payload)?;
    Ok(())
}

fn copy_pty_output(
    mut reader: Box<dyn Read + Send>,
    shared: Arc<Shared>,
) -> io::Result<()> {
    let mut buffer = [0_u8; IO_CHUNK_BYTES];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(read) => shared.publish_output(&buffer[..read]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
}

#[derive(Debug)]
enum ServerCommand {
    Resize { cols: u16, rows: u16 },
    Kill,
}

struct Shared {
    pty_writer: Mutex<Box<dyn Write + Send>>,
    scrollback: Mutex<Scrollback>,
    active: Mutex<Option<ActiveClient>>,
    next_client_id: AtomicU64,
}

impl Shared {
    fn new(writer: Box<dyn Write + Send>, scrollback_bytes: usize) -> Self {
        Self {
            pty_writer: Mutex::new(writer),
            scrollback: Mutex::new(Scrollback::new(scrollback_bytes)),
            active: Mutex::new(None),
            next_client_id: AtomicU64::new(1),
        }
    }

    fn write_input(&self, bytes: &[u8]) -> Result<()> {
        let mut writer = lock(&self.pty_writer);
        writer.write_all(bytes)?;
        writer.flush()?;
        Ok(())
    }

    fn publish_output(&self, bytes: &[u8]) {
        let mut scrollback = lock(&self.scrollback);
        scrollback.push(bytes);
        let mut active = lock(&self.active);
        let Some(client) = active.as_mut() else {
            return;
        };
        let mut writer = lock(&client.writer);
        if write_frame(&mut *writer, kind::OUTPUT, bytes).is_err() {
            let _ = writer.shutdown(Shutdown::Both);
            drop(writer);
            *active = None;
        }
    }

    fn attach(&self, stream: &UnixStream) -> Result<u64> {
        let scrollback = lock(&self.scrollback);
        let snapshot = scrollback.snapshot();
        let mut active = lock(&self.active);

        if let Some(previous) = active.take() {
            let writer = lock(&previous.writer);
            let _ = writer.shutdown(Shutdown::Both);
        }

        let id = self.next_client_id.fetch_add(1, Ordering::Relaxed);
        let writer = Arc::new(Mutex::new(stream.try_clone()?));
        {
            let mut target = lock(&writer);
            write_frame(&mut *target, kind::ATTACHED, b"")?;
            for chunk in snapshot.chunks(FRAME_CHUNK_BYTES) {
                write_frame(&mut *target, kind::OUTPUT, chunk)?;
            }
        }
        *active = Some(ActiveClient {
            id,
            writer: Arc::clone(&writer),
        });
        Ok(id)
    }

    fn is_active(&self, id: u64) -> bool {
        lock(&self.active)
            .as_ref()
            .is_some_and(|client| client.id == id)
    }

    fn detach(&self, id: u64) {
        let mut active = lock(&self.active);
        if active.as_ref().is_some_and(|client| client.id == id) {
            if let Some(client) = active.take() {
                let writer = lock(&client.writer);
                let _ = writer.shutdown(Shutdown::Both);
            }
        }
    }

    fn broadcast(&self, kind: u8, payload: &[u8]) {
        let mut active = lock(&self.active);
        let Some(client) = active.as_mut() else {
            return;
        };
        let mut writer = lock(&client.writer);
        if write_frame(&mut *writer, kind, payload).is_err() {
            let _ = writer.shutdown(Shutdown::Both);
            drop(writer);
            *active = None;
        }
    }

    fn disconnect_active(&self) {
        let mut active = lock(&self.active);
        if let Some(client) = active.take() {
            let writer = lock(&client.writer);
            let _ = writer.shutdown(Shutdown::Both);
        }
    }
}

struct ActiveClient {
    id: u64,
    writer: Arc<Mutex<UnixStream>>,
}

struct Scrollback {
    bytes: VecDeque<u8>,
    max_bytes: usize,
}

impl Scrollback {
    fn new(max_bytes: usize) -> Self {
        Self {
            bytes: VecDeque::with_capacity(max_bytes.min(64 * 1024)),
            max_bytes,
        }
    }

    fn push(&mut self, bytes: &[u8]) {
        if self.max_bytes == 0 {
            return;
        }
        if bytes.len() >= self.max_bytes {
            self.bytes.clear();
            self.bytes
                .extend(bytes[bytes.len() - self.max_bytes..].iter().copied());
            return;
        }
        let overflow = self
            .bytes
            .len()
            .saturating_add(bytes.len())
            .saturating_sub(self.max_bytes);
        self.bytes.drain(..overflow);
        self.bytes.extend(bytes.iter().copied());
    }

    fn snapshot(&self) -> Vec<u8> {
        self.bytes.iter().copied().collect()
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}
