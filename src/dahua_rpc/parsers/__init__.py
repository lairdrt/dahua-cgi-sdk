"""
CGI response parsers.
"""

from .camera import parse_cameras, parse_stream_profiles
from .cgi import parse_cgi_items, parse_cgi_properties

__all__ = [
    "parse_cameras",
    "parse_cgi_items",
    "parse_cgi_properties",
    "parse_stream_profiles",
]
