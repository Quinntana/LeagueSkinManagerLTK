// SPDX-License-Identifier: MIT OR Apache-2.0

use std::borrow::Cow;

/// An error safe to return across the sidecar protocol boundary.
#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct EngineError {
    code: Cow<'static, str>,
    message: String,
}

impl EngineError {
    pub fn new(code: impl Into<Cow<'static, str>>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self::new("invalid_request", message)
    }

    pub fn invalid_params(message: impl Into<String>) -> Self {
        Self::new("invalid_params", message)
    }

    pub fn invalid_path(message: impl Into<String>) -> Self {
        Self::new("invalid_path", message)
    }

    pub fn code(&self) -> &str {
        &self.code
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}
