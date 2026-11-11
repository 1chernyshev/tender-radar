"""Безопасная классификация сетевых ошибок без URL и токенов."""
import socket
import ssl
import urllib.error


def error_kind(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, socket.gaierror):
        return 'DNS'
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'TLS certificate'
    if isinstance(reason, ssl.SSLError):
        return 'TLS'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(reason, ConnectionError):
        return 'connection'
    return type(reason).__name__
