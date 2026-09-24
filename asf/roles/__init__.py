"""asf.roles — the identity a session works under, one file per role.

A role is a file, ``asf/roles/<name>.md``: a two-key frontmatter (``name``, ``purpose``) and five
sections in a fixed order —

* **Identity** — who the session is for the length of its job;
* **Doctrine** — four to eight rules, each carrying the incident that made it a rule;
* **Output** — the one side file the role produces (the brief's tail owns the report envelope);
* **Economy** — what the role may open before it is paying to read, six lines at most;
* **Boundaries** — what the role writes and what it never touches.

A role is *not* a model, a backend, an effort or a tool list. Those are the operator's
configuration and the phase's, and a role file that names one is refused (:func:`validate`), so
the identity stays the same on every install and the cost stays the operator's to set.

Nothing here is a launch-time choice: :data:`asf.roles.roles.BINDINGS` maps every brief kind to a
role as data, and the loader is read the same way by the brief, the gate and ``asf doctor``.
"""
