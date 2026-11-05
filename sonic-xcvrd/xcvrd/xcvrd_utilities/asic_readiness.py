"""
    asic_readiness
    Registry of the front-end ASIC namespaces whose APPL_DB PORT_TABLE has published a readiness
    marker (PortConfigDone / PortInitDone), and of the namespaces the publishing workers have
    published since. Written by the xcvrd main thread and the publishing workers, read by the workers.
"""

try:
    import threading

    from sonic_py_common import multi_asic
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


class AsicReadiness:
    """Which front-end namespaces have published a marker key (ready), and which of them every
    publishing worker has published (admitted).

    `ready` is written by the xcvrd main thread (mark_ready()); each publishing worker declares itself
    (register_publisher(), from the main thread that constructs it) and marks the namespaces it has
    published (mark_admitted()). Every method is thread-safe. Consumers poll the registry in their own
    loops; there is deliberately no subscription or notification mechanism.
    """

    def __init__(self, namespaces):
        self._all = frozenset(namespaces)
        # Resolved once: multi_asic.get_asic_index_from_namespace() re-reads asic.conf on every call.
        self._ns_by_asic = {multi_asic.get_asic_index_from_namespace(ns): ns for ns in self._all}
        self._ready = set()
        # publishing worker name -> namespaces it has published: exempt when ready at its construction (their
        # start-up pass runs in its init(), concurrently with the module-writing workers as today), marked after
        # the admission pass otherwise
        self._admitted = {}
        self._lock = threading.Lock()

    @classmethod
    def all_ready_registry(cls, namespaces):
        """Registry with every namespace already ready (default for callers that do not gate)."""
        registry = cls(namespaces)
        for ns in namespaces:
            registry.mark_ready(ns)
        return registry

    def mark_ready(self, namespace):
        """Register a namespace as ready. Idempotent; ValueError for an unknown namespace."""
        if namespace not in self._all:
            raise ValueError("unknown namespace {!r}; known: {}".format(namespace, sorted(self._all)))
        with self._lock:
            self._ready.add(namespace)

    def is_ready(self, namespace):
        """True once the namespace is registered ready; False for a pending or unknown namespace."""
        with self._lock:
            return namespace in self._ready

    def register_publisher(self, name, admitted=()):
        """Declare a publishing worker; `admitted` are the namespaces ready when it was constructed."""
        with self._lock:
            self._admitted.setdefault(name, set()).update(admitted)

    def mark_admitted(self, name, namespace):
        """Record that the publishing worker `name` has published the namespace. KeyError for a publisher
        that did not declare itself: a silent declaration would hide a wiring bug."""
        with self._lock:
            self._admitted[name].add(namespace)

    def admitted_namespaces(self):
        """Ready namespaces every declared publishing worker has published (exempt at its declaration, or
        marked after its admission pass). Every publisher counts: each pass covers every port of the
        namespace and each worker soaks its own insertion events. With no publisher declared this is the
        ready set (callers that do not gate, and the gated workers' first iterations)."""
        with self._lock:
            result = set(self._ready)
            for done in self._admitted.values():
                result &= done
            return frozenset(result)

    def _attributed(self, asic_id, namespaces):
        """True when asic_id's namespace is in `namespaces`. An id that maps to no managed namespace (None or
        unknown) cannot be attributed to one namespace: such a port waits for every namespace, the rule that
        applied to every port before readiness was tracked per namespace."""
        ns = self._ns_by_asic.get(asic_id)
        return ns in namespaces if ns is not None else len(namespaces) == len(self._all)

    def is_asic_ready(self, asic_id):
        """Gate helper of the workers that wait for the namespace's marker."""
        return self._attributed(asic_id, self.ready_namespaces())

    def is_asic_admitted(self, asic_id, admitted=None):
        """Gate helper of the module-writing workers, which wait for the namespace's publication.

        `admitted` is a set from admitted_namespaces() to evaluate against instead of the live registry: a
        worker whose per-iteration cache must not predate an admission passes the set it took before reading
        the cache, so that a namespace admitted later waits for the next iteration."""
        return self._attributed(asic_id, self.admitted_namespaces() if admitted is None else admitted)

    def namespace_for_asic(self, asic_id):
        """Namespace of a managed ASIC id, None when the id is not managed by this registry."""
        return self._ns_by_asic.get(asic_id)

    def all_ready(self):
        with self._lock:
            return len(self._ready) == len(self._all)

    def ready_namespaces(self):
        with self._lock:
            return frozenset(self._ready)


def log_first_deferral(registry, logged, asic_id, worker, lport, log_notice):
    """Log PORTS_DEFERRED once per namespace for a worker that skips lport while its namespace is
    pending; `logged` is the worker's own set of namespaces already reported."""
    ns = registry.namespace_for_asic(asic_id)
    if ns not in logged:
        logged.add(ns)
        log_notice("[{}] PORTS_DEFERRED ({}): namespace pending, first deferred port {}".format(
            ns if ns is not None else "asic id {}".format(asic_id), worker, lport))
