# ruff: noqa  (kept as run on xzgpu 2026-10-29; this header line is the only addition)
"""P-SHRINK instrumentation: inclusive per-call phase timers by monkeypatching.
Untimed diagnostic; changes no behaviour (wrappers call through)."""
import fcntl, gc, os, time

pc = time.perf_counter
CUR: dict[str, float] = {}
CNT: dict[str, int] = {}
_installed = False


def _acc(label, dt):
    CUR[label] = CUR.get(label, 0.0) + dt * 1000.0
    CNT[label] = CNT.get(label, 0) + 1


def _wrap(owner, attr, label, kind="fn"):
    if kind == "cm":  # classmethod: wrap the bound callable, install as staticmethod
        orig = getattr(owner, attr)
    else:
        orig = owner.__dict__[attr] if isinstance(owner, type) else getattr(owner, attr)

    def w(*a, **k):
        t = pc()
        try:
            return orig(*a, **k)
        finally:
            _acc(label, pc() - t)

    w.__name__ = getattr(orig, "__name__", attr)
    setattr(owner, attr, staticmethod(w) if kind == "cm" else w)


_gc_t = [0.0]


def _gc_cb(phase, info):
    if phase == "start":
        _gc_t[0] = pc()
    else:
        _acc("gc", pc() - _gc_t[0])
        _acc("gc_gen%d" % info.get("generation", -1), pc() - _gc_t[0])


def install():
    global _installed
    if _installed:
        return
    _installed = True
    import tgms.artifact.refresh as R
    import tgms.artifact.registry as G
    import tgms.temporal.algebra as A
    import tgms.tgir.evaluate as E
    import tgms.tgir.ttq as TQ
    from tgms.agent.executor import ResultStore
    from tgms.tgir.depscope import DependencyScope
    from tgms.tools.server import ToolRouter

    # execution
    _wrap(R, "_run_operator", "exec")
    _wrap(ToolRouter, "call", "exec.router_call")
    _wrap(ToolRouter, "leaf_meta", "exec.leaf_meta")
    _wrap(A, "digest", "exec.result_digest")
    _wrap(A, "_canonicalize_floats", "exec.canonicalize")
    _wrap(E, "evaluate_leaf", "exec.kernel")
    _wrap(TQ, "envelope_metadata", "exec.envelope_metadata")
    # publication
    _wrap(R, "_publish", "publish")
    _wrap(R, "_advance_parents", "publish.advance_parents")
    _wrap(DependencyScope, "from_json", "publish.dep_from_json", kind="cm")
    _wrap(ResultStore, "put", "publish.result_put")
    _wrap(G.Registry, "register", "publish.register")
    _wrap(G.Registry, "_stamp_blob_sha256", "publish.register.stamp")
    _wrap(G.Registry, "_store_identity", "publish.store_identity")
    _wrap(G.Registry, "append", "publish.append")
    _wrap(G.Registry, "_append_locked", "publish.append.locked")
    _wrap(G.Registry, "_reload_tail_locked", "publish.append.reload_tail")
    _wrap(G.Registry, "_append_to_index", "publish.append.fold_index")
    _wrap(G.Registry, "_verify_append_landed", "publish.append.verify_landed")
    _wrap(G, "canonical_json", "publish.append.canonical_json")
    _wrap(G, "extend_chain", "publish.append.extend_chain")
    _wrap(os, "fsync", "publish.append.fsync")
    _wrap(fcntl, "flock", "publish.append.flock")
    gc.callbacks.append(_gc_cb)


def reset():
    CUR.clear()
    CNT.clear()


def snapshot():
    return dict(CUR), dict(CNT)
