"""Abstract base class for router adapters."""
import time
from abc import ABC, abstractmethod


class NotSupportedError(Exception):
    """Raised when an operation is not supported by this adapter."""
    pass


class RouterAdapter(ABC):
    """Base class for all router adapters.

    Each adapter wraps a brand-specific library and exposes a uniform
    synchronous interface that the Flask routes consume.
    """

    brand: str = "unknown"
    supports_inbox: bool = True
    supports_outbox: bool = True

    # --- Required methods ---

    @abstractmethod
    def send_sms(self, numbers: list, message: str) -> None:
        """Send an SMS to one or more numbers."""

    @abstractmethod
    def get_inbox(self, page: int = 1, per_page: int = 20) -> dict:
        """Return inbox messages.

        Returns:
            {'messages': [...], 'page': int, 'has_more': bool}
            Each message dict: {'Index', 'Phone', 'Content', 'Date'}
        """

    @abstractmethod
    def get_outbox(self, page: int = 1, per_page: int = 50) -> dict:
        """Return outbox (sent) messages.

        Raises NotSupportedError if the router does not expose sent messages.
        Returns same shape as get_inbox.
        """

    @abstractmethod
    def delete_sms(self, index) -> None:
        """Delete a single SMS by its index/id."""

    @abstractmethod
    def get_status(self) -> dict:
        """Return router status.

        Returns:
            {'status': 'ok', 'signal_bars': int, 'operator': str, 'network': str}
        """

    @abstractmethod
    def check_health(self) -> dict:
        """Check that the router is reachable.

        Returns:
            {'status': 'ok', 'router': 'reachable'}
        Raises an exception if the router cannot be reached.
        """

    # --- Optional helpers with default implementations ---

    def close(self) -> None:
        """Release any persistent resources (connections, background threads…).

        Called when the adapter is discarded (config reload, connectivity
        test). Default is a no-op; override in adapters that keep state
        alive across calls (e.g. a persistent session or event loop).
        """

    def delete_sms_batch(self, indices: list) -> int:
        """Delete multiple SMS. Returns count deleted.

        Default implementation loops over delete_sms().
        Adapters may override this for more efficient batch operations.
        """
        for idx in indices:
            self.delete_sms(idx)
            time.sleep(0.1)
        return len(indices)

    def delete_outbox_all(self, on_progress=None) -> int:
        """Delete all sent messages iteratively.

        Args:
            on_progress: optional callable(deleted_count) called after each batch.
        Returns total count of deleted messages.
        Raises NotSupportedError if outbox is not supported.
        """
        if not self.supports_outbox:
            raise NotSupportedError(f"{self.brand} ne supporte pas la boîte d'envoi.")

        deleted = 0
        per_page = 50

        while True:
            result = self.get_outbox(page=1, per_page=per_page)
            indices = [m['Index'] for m in result['messages'] if m.get('Index')]
            if not indices:
                break
            self.delete_sms_batch(indices)
            deleted += len(indices)
            if on_progress:
                on_progress(deleted)
            if len(indices) < per_page:
                break
            time.sleep(0.5)

        return deleted
