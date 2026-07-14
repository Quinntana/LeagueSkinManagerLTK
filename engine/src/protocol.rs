// SPDX-License-Identifier: MIT OR Apache-2.0

use crate::error::EngineError;
use crate::overlay::{BuildOverlayParams, build_overlay};
use crate::package::{InspectPackageParams, inspect_package};
use crate::provider::{ProviderSmokeParams, smoke_provider};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
use std::sync::{Arc, Mutex};

pub const PROTOCOL_VERSION: u32 = 1;
const MAX_REQUEST_LINE_BYTES: usize = 1024 * 1024;
const MAX_REQUEST_ID_BYTES: usize = 256;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: u32,
    id: String,
    method: String,
    params: Value,
}

#[derive(Serialize)]
struct SuccessResponse<'a, T> {
    protocol: u32,
    id: &'a str,
    #[serde(rename = "type")]
    message_type: &'static str,
    ok: bool,
    result: T,
}

#[derive(Serialize)]
struct ErrorResponse<'a> {
    protocol: u32,
    id: &'a str,
    #[serde(rename = "type")]
    message_type: &'static str,
    ok: bool,
    error: ErrorBody<'a>,
}

#[derive(Serialize)]
struct ErrorBody<'a> {
    code: &'a str,
    message: &'a str,
}

#[derive(Serialize)]
struct EventMessage<'a, T> {
    protocol: u32,
    id: &'a str,
    #[serde(rename = "type")]
    message_type: &'static str,
    event: &'a str,
    data: T,
}

/// Synchronized NDJSON output. Clones share the same writer, which lets LTK's
/// progress callback safely emit from worker threads without interleaving lines.
pub struct Emitter<W> {
    writer: Arc<Mutex<W>>,
}

impl<W> Clone for Emitter<W> {
    fn clone(&self) -> Self {
        Self {
            writer: Arc::clone(&self.writer),
        }
    }
}

impl<W> Emitter<W>
where
    W: Write,
{
    pub fn new(writer: W) -> Self {
        Self {
            writer: Arc::new(Mutex::new(writer)),
        }
    }

    fn line<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let mut serialized = serde_json::to_vec(message)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        serialized.push(b'\n');
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| io::Error::other("protocol output lock was poisoned"))?;
        writer.write_all(&serialized)?;
        writer.flush()
    }

    fn success<T: Serialize>(&self, id: &str, result: T) -> io::Result<()> {
        self.line(&SuccessResponse {
            protocol: PROTOCOL_VERSION,
            id,
            message_type: "response",
            ok: true,
            result,
        })
    }

    fn error(&self, id: &str, error: &EngineError) -> io::Result<()> {
        self.line(&ErrorResponse {
            protocol: PROTOCOL_VERSION,
            id,
            message_type: "response",
            ok: false,
            error: ErrorBody {
                code: error.code(),
                message: error.message(),
            },
        })
    }

    pub fn event<T: Serialize>(&self, id: &str, event: &str, data: &T) -> io::Result<()> {
        self.line(&EventMessage {
            protocol: PROTOCOL_VERSION,
            id,
            message_type: "event",
            event,
            data,
        })
    }
}

pub fn serve<R, W>(mut reader: R, emitter: Emitter<W>) -> io::Result<()>
where
    R: BufRead,
    W: Write + Send + 'static,
{
    loop {
        match read_bounded_line(&mut reader, MAX_REQUEST_LINE_BYTES)? {
            BoundedLine::Eof => return Ok(()),
            BoundedLine::TooLong => {
                emitter.error(
                    "",
                    &EngineError::invalid_request(format!(
                        "request line exceeds {MAX_REQUEST_LINE_BYTES} bytes"
                    )),
                )?;
            }
            BoundedLine::Line(mut bytes) => {
                while matches!(bytes.last(), Some(b'\r' | b'\n')) {
                    bytes.pop();
                }
                if bytes.iter().all(u8::is_ascii_whitespace) {
                    continue;
                }
                process_line(&bytes, &emitter)?;
            }
        }
    }
}

fn process_line<W>(line: &[u8], emitter: &Emitter<W>) -> io::Result<()>
where
    W: Write + Send + 'static,
{
    let request = match serde_json::from_slice::<Request>(line) {
        Ok(request) => request,
        Err(error) => {
            let id = recover_request_id(line);
            return emitter.error(
                &id,
                &EngineError::invalid_request(format!("invalid request JSON: {error}")),
            );
        }
    };
    let id = request.id.clone();
    let result = dispatch(request, emitter);
    match result {
        Ok(value) => emitter.success(&id, value),
        Err(error) => emitter.error(&id, &error),
    }
}

fn dispatch<W>(request: Request, emitter: &Emitter<W>) -> Result<Value, EngineError>
where
    W: Write + Send + 'static,
{
    validate_request(&request)?;
    match request.method.as_str() {
        "engine.hello" => {
            let _: EmptyParams = decode_params(request.params)?;
            Ok(hello())
        }
        "package.inspect" => {
            let params: InspectPackageParams = decode_params(request.params)?;
            serialize_result(inspect_package(&params.path)?)
        }
        "overlay.build" => {
            let params: BuildOverlayParams = decode_params(request.params)?;
            serialize_result(build_overlay(params, &request.id, emitter)?)
        }
        "provider.smoke" => {
            let params: ProviderSmokeParams = decode_params(request.params)?;
            serialize_result(smoke_provider(params)?)
        }
        _ => Err(EngineError::new(
            "method_not_found",
            format!("unknown method '{}'", request.method),
        )),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyParams {}

fn validate_request(request: &Request) -> Result<(), EngineError> {
    if request.protocol != PROTOCOL_VERSION {
        return Err(EngineError::new(
            "unsupported_protocol",
            format!(
                "unsupported protocol {}; expected {PROTOCOL_VERSION}",
                request.protocol
            ),
        ));
    }
    if request.id.is_empty() {
        return Err(EngineError::invalid_request("request id must not be empty"));
    }
    if request.id.len() > MAX_REQUEST_ID_BYTES {
        return Err(EngineError::invalid_request(format!(
            "request id exceeds {MAX_REQUEST_ID_BYTES} bytes"
        )));
    }
    if request.id.chars().any(char::is_control) {
        return Err(EngineError::invalid_request(
            "request id must not contain control characters",
        ));
    }
    if request.method.trim().is_empty() {
        return Err(EngineError::invalid_request(
            "request method must not be empty",
        ));
    }
    if !request.params.is_object() {
        return Err(EngineError::invalid_params(
            "request params must be a JSON object",
        ));
    }
    Ok(())
}

fn decode_params<T: DeserializeOwned>(params: Value) -> Result<T, EngineError> {
    serde_json::from_value(params)
        .map_err(|error| EngineError::invalid_params(format!("invalid method params: {error}")))
}

fn serialize_result<T: Serialize>(result: T) -> Result<Value, EngineError> {
    serde_json::to_value(result).map_err(|error| {
        EngineError::new(
            "internal_error",
            format!("failed to serialize method result: {error}"),
        )
    })
}

fn hello() -> Value {
    json!({
        "engine_name": "ltk-engine",
        "engine_version": env!("CARGO_PKG_VERSION"),
        "protocol_version": PROTOCOL_VERSION,
        "methods": [
            "engine.hello",
            "package.inspect",
            "overlay.build",
            "provider.smoke"
        ],
        "package_formats": ["modpkg", "fantome"],
        "crate_versions": {
            "ltk_overlay": "0.5.2",
            "ltk_modpkg": "0.6.0",
            "ltk_fantome": "0.6.1"
        },
        "provider": {
            "mode": "configuration_only",
            "binaries_bundled": false,
            "downloads_binaries": false,
            "starts_processes": false,
            "anti_hack_enforced": true
        },
        "load_order": "first_enabled_package_has_highest_priority"
    })
}

fn recover_request_id(line: &[u8]) -> String {
    serde_json::from_slice::<Value>(line)
        .ok()
        .and_then(|value| value.get("id").and_then(Value::as_str).map(str::to_owned))
        .unwrap_or_default()
}

enum BoundedLine {
    Eof,
    Line(Vec<u8>),
    TooLong,
}

fn read_bounded_line<R: BufRead>(reader: &mut R, maximum_bytes: usize) -> io::Result<BoundedLine> {
    let mut line = Vec::new();
    let mut too_long = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if line.is_empty() && !too_long {
                Ok(BoundedLine::Eof)
            } else if too_long {
                Ok(BoundedLine::TooLong)
            } else {
                Ok(BoundedLine::Line(line))
            };
        }

        let newline = available.iter().position(|byte| *byte == b'\n');
        let take = newline.map_or(available.len(), |index| index + 1);
        if !too_long {
            if line.len().saturating_add(take) > maximum_bytes {
                too_long = true;
                line.clear();
            } else {
                line.extend_from_slice(&available[..take]);
            }
        }
        reader.consume(take);

        if newline.is_some() {
            return if too_long {
                Ok(BoundedLine::TooLong)
            } else {
                Ok(BoundedLine::Line(line))
            };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn bounded_reader_recovers_after_oversized_line() {
        let mut input = vec![b'x'; 12];
        input.extend_from_slice(b"\nok\n");
        let mut cursor = Cursor::new(input);
        assert!(matches!(
            read_bounded_line(&mut cursor, 8).unwrap(),
            BoundedLine::TooLong
        ));
        match read_bounded_line(&mut cursor, 8).unwrap() {
            BoundedLine::Line(line) => assert_eq!(line, b"ok\n"),
            _ => panic!("expected the next bounded line"),
        }
    }

    #[test]
    fn hello_declares_security_boundary() {
        let value = hello();
        assert_eq!(value["provider"]["binaries_bundled"], false);
        assert_eq!(value["provider"]["anti_hack_enforced"], true);
    }
}
