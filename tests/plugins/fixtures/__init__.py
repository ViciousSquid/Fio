"""Plugin packages written for the contract suite.

Each sub-package is a real Fio plugin — it exposes a module-level ``PLUGIN``
(or ``get_plugin()``) exactly as a shipped plugin does — but each is built to
exercise one part of the contract, including the parts that are meant to fail:
a plugin that raises during ``register``, one whose ``PLUGIN`` is the wrong
type, one that declares a newer API version than the host provides.

They live under ``tests/`` rather than ``plugins/`` so nothing here is
discovered by a real Fio launch, packaged into an export, or shipped to a user.
The discovery tests point the manager at this directory explicitly.
"""
