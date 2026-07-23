"""Auto-imported by Python at startup (this dir is on the child's PYTHONPATH).

Only acts when the CLI marked the environment. Any failure is swallowed so the
job runs exactly as it would without wherewent.

TODO: chaining the site's original sitecustomize is out of scope for this
prototype (if the environment already shipped one, it is shadowed here).
"""

import os

if os.environ.get("WHEREWENT_ACTIVE") == "1":
    try:
        from wherewent.recorder import install_from_env

        install_from_env()
    except Exception:
        pass
