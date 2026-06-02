"""Signal handlers — split by domain so additions stay isolated.

``apps.JobHuntingConfig.ready()`` imports this package and the act of
importing the modules below registers each ``@receiver`` decorator.
Keep imports side-effect-only — anything that has to run on app start
belongs in ``ready()`` itself, not here.
"""
from . import resume  # noqa: F401
from . import federation  # noqa: F401
