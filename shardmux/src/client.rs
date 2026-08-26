use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use crossterm::terminal;

use crate::model::SessionRecord;
use crate::protocol::{encode_resize, kind, read_frame, write_frame};

pub fn ping(record: &SessionRecord, timeout: Duration) -> Result<()> {
    let mut stream = connect(record, timeout)?;
    write_frame(&mut stream, kind::PING, b"")?;
    let frame = read_frame(&mut stream)?.context("session closed the ping connection")?;
    if frame.kind != kind::PONG {
        bail!("unexpected ping response type {}", frame.kind);
    }
    Ok(())
}

pub fn is_live(record: &SessionRecord) -> bool {
    ping(record, Duration::from_millis(200)).is_ok()
}

pub fn inject(record: &SessionRecord, bytes: &[u8]) -> Result<()> {
    let mut stream = connect(record, Duration::from_secs(2))?;
    write_frame(&mut stream, kind::INJECT, bytes)?;
    expect_ack(&mut stream, "send")
}

pub fn request_kill(record: &SessionRecord) -> Result<()> {
    let mut stream = connect(record, Duration::from_secs(2))?;
    write_frame(&mut stream, kind::KILL, b"")?;
    expect_ack(&mut stream, "kill")
}

pub fn attach(record: &SessionRecord) -> Result<()> {
    let stream = connect(record, Duration::from_secs(2))?;
    stream.set_read_timeout(None)?;
    stream.set_write_timeout(None)?;

    terminal::enable_raw_mode().context("failed to put the terminal into raw mode")?;
    let _raw_mode = RawModeGuard;

    let mut reader = stream.try_clone()?;
    let writer = Arc::new(Mutex::new(stream));
    {
        let mut target = lock(&writer);
        write_frame(&mut *target, kind::ATTACH, b"")?;
        let (cols, rows) = terminal::size().unwrap_or((80, 24));
        write_frame(&mut *target, kind::RESIZE, &encode_resize(cols, rows))?;
    }

    let done = Arc::new(AtomicBool::new(false));
    spawn_input_thread(Arc::clone(&writer), Arc::clone(&done));
    spawn_resize_thread(Arc::clone(&writer), Arc::clone(&done));

    let mut stdout = io::stdout().lock();
    loop {
        let frame = match read_frame(&mut reader) {
            Ok(Some(frame)) => frame,
            Ok(None) => break,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe
                ) =>
            {
                break;
            }
            Err(error) => return Err(error).context("attachment connection failed"),
        };
        match frame.kind {
            kind::ATTACHED | kind::ACK => {}
            kind::OUTPUT => {
                stdout.write_all(&frame.payload)?;
                stdout.flush()?;
            }
            kind::EXIT => {
                stdout.write_all(b"\r\n")?;
                stdout.flush()?;
                eprintln!("shardmux: {}", String::from_utf8_lossy(&frame.payload));
                break;
            }
            kind::ERROR => {
                bail!("session server: {}", String::from_utf8_lossy(&frame.payload));
            }
            other => bail!("unexpected server frame type {other}"),
        }
    }

    done.store(true, Ordering::Relaxed);
    let _ = lock(&writer).shutdown(Shutdown::Both);
    Ok(())
}

fn connect(record: &SessionRecord, timeout: Duration) -> Result<UnixStream> {
    let stream = UnixStream::connect(&record.socket).with_context(|| {
        format!(
            "cannot connect to session '{}' at {}",
            record.name,
            record.socket.display()
        )
    })?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    Ok(stream)
}

fn expect_ack(stream: &mut UnixStream, operation: &str) -> Result<()> {
    let frame = read_frame(stream)?.context("session closed the control connection")?;
    match frame.kind {
        kind::ACK => Ok(()),
        kind::ERROR => bail!(
            "session rejected {operation}: {}",
            String::from_utf8_lossy(&frame.payload)
        ),
        other => bail!("unexpected response type {other} while waiting for {operation}"),
    }
}

fn spawn_input_thread(writer: Arc<Mutex<UnixStream>>, done: Arc<AtomicBool>) {
    thread::spawn(move || {
        let mut input = io::stdin().lock();
        let mut buffer = [0_u8; 1024];
        let mut prefix = false;
        while !done.load(Ordering::Relaxed) {
            let read = match input.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => read,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            };
            let mut outgoing = Vec::with_capacity(read + 1);
            for &byte in &buffer[..read] {
                if prefix {
                    match byte {
                        b'd' => {
                            let _ = write_locked(&writer, kind::DETACH, b"");
                            done.store(true, Ordering::Relaxed);
                            return;
                        }
                        0x02 => outgoing.push(0x02),
                        other => {
                            outgoing.push(0x02);
                            outgoing.push(other);
                        }
                    }
                    prefix = false;
                } else if byte == 0x02 {
                    prefix = true;
                } else {
                    outgoing.push(byte);
                }
            }
            if !outgoing.is_empty()
                && write_locked(&writer, kind::INPUT, &outgoing).is_err()
            {
                done.store(true, Ordering::Relaxed);
                return;
            }
        }
    });
}

fn spawn_resize_thread(writer: Arc<Mutex<UnixStream>>, done: Arc<AtomicBool>) {
    thread::spawn(move || {
        let mut previous = terminal::size().unwrap_or((80, 24));
        while !done.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(200));
            let current = terminal::size().unwrap_or(previous);
            if current != previous {
                if write_locked(
                    &writer,
                    kind::RESIZE,
                    &encode_resize(current.0, current.1),
                )
                .is_err()
                {
                    done.store(true, Ordering::Relaxed);
                    return;
                }
                previous = current;
            }
        }
    });
}

fn write_locked(writer: &Mutex<UnixStream>, kind: u8, payload: &[u8]) -> io::Result<()> {
    let mut writer = lock(writer);
    write_frame(&mut *writer, kind, payload)
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

struct RawModeGuard;

impl Drop for RawModeGuard {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
    }
}
