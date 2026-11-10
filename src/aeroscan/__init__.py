"""Aero-Scan: flight anomaly detection on open ADS-B data."""

from .detector import Detector
from .models import Alert, Severity, StateVector

__all__ = ["Alert", "Detector", "Severity", "StateVector"]
__version__ = "0.1.0"
