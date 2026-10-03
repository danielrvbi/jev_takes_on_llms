"""flock-compatible portable locks; retain the existing POSIX lock protocol."""
try:
    import fcntl
except ImportError:  # Windows has no fcntl.
    import portalocker

    class _PortableFlock:
        LOCK_SH = portalocker.LOCK_SH
        LOCK_EX = portalocker.LOCK_EX
        LOCK_NB = portalocker.LOCK_NB
        LOCK_UN = portalocker.LOCK_UN

        @staticmethod
        def flock(handle, flags):
            try:
                if flags == portalocker.LOCK_UN:
                    portalocker.unlock(handle)
                else:
                    portalocker.lock(handle, flags)
            except portalocker.exceptions.LockException as exc:
                raise BlockingIOError("File is locked by another process") from exc

    fcntl = _PortableFlock()
