use std::io::{self, Read, Write};

use anyhow::{Result, bail};

pub const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

pub mod kind {
    pub const ATTACH: u8 = 1;
    pub const INPUT: u8 = 2;
    pub const RESIZE: u8 = 3;
    pub const DETACH: u8 = 4;
    pub const PING: u8 = 5;
    pub const INJECT: u8 = 6;
    pub const KILL: u8 = 7;

    pub const OUTPUT: u8 = 64;
    pub const ATTACHED: u8 = 65;
    pub const ACK: u8 = 66;
    pub const ERROR: u8 = 67;
    pub const PONG: u8 = 68;
    pub const EXIT: u8 = 69;
}

#[derive(Debug)]
pub struct Frame {
    pub kind: u8,
    pub payload: Vec<u8>,
}

pub fn write_frame(writer: &mut impl Write, kind: u8, payload: &[u8]) -> io::Result<()> {
    if payload.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "frame exceeds maximum size",
        ));
    }
    writer.write_all(&[kind])?;
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)?;
    writer.flush()?;
    Ok(())
}

pub fn read_frame(reader: &mut impl Read) -> io::Result<Option<Frame>> {
    let mut kind = [0_u8; 1];
    match reader.read(&mut kind) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!(),
        Err(error) if error.kind() == io::ErrorKind::Interrupted => return read_frame(reader),
        Err(error) => return Err(error),
    }

    let mut length = [0_u8; 4];
    reader.read_exact(&mut length)?;
    let length = u32::from_be_bytes(length) as usize;
    if length > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "peer sent an oversized frame",
        ));
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload)?;
    Ok(Some(Frame {
        kind: kind[0],
        payload,
    }))
}

pub fn encode_resize(cols: u16, rows: u16) -> [u8; 4] {
    let [c0, c1] = cols.to_be_bytes();
    let [r0, r1] = rows.to_be_bytes();
    [c0, c1, r0, r1]
}

pub fn decode_resize(payload: &[u8]) -> Result<(u16, u16)> {
    if payload.len() != 4 {
        bail!("resize frame must contain exactly four bytes");
    }
    let cols = u16::from_be_bytes([payload[0], payload[1]]);
    let rows = u16::from_be_bytes([payload[2], payload[3]]);
    if cols == 0 || rows == 0 {
        bail!("terminal dimensions must be non-zero");
    }
    Ok((cols, rows))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_round_trip() {
        let mut bytes = Vec::new();
        write_frame(&mut bytes, kind::INPUT, b"hello").unwrap();
        let frame = read_frame(&mut bytes.as_slice()).unwrap().unwrap();
        assert_eq!(frame.kind, kind::INPUT);
        assert_eq!(frame.payload, b"hello");
    }

    #[test]
    fn resize_round_trip() {
        assert_eq!(decode_resize(&encode_resize(120, 40)).unwrap(), (120, 40));
    }
}
