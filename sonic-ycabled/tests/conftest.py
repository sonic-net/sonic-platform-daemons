import site
import sys


def _prefer_newer_python_sites() -> None:
    """Ensure test imports prefer user/local site-packages over distro site-packages."""
    preferred = []

    try:
        preferred.append(site.getusersitepackages())
    except Exception:
        pass

    for path in list(sys.path):
        if path.startswith("/usr/local/lib/python") and path.endswith("/dist-packages"):
            preferred.append(path)

    ordered = []
    seen = set()
    for path in preferred:
        if path and path not in seen:
            ordered.append(path)
            seen.add(path)

    for path in reversed(ordered):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


_prefer_newer_python_sites()
