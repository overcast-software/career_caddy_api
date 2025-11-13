import logging
from job_hunting.lib.models.base import BaseModel

logger = logging.getLogger(__name__)


class SQLAlchemySessionMiddleware:
    """
    Middleware to ensure proper SQLAlchemy session lifecycle management.

    - On exceptions: rollback and remove the scoped session
    - On request completion: clear and remove the session
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
            return response
        except Exception:
            # Clean up session on any exception
            BaseModel.cleanup_session_on_exception()
            raise
        finally:
            # Always clean up session at end of request
            BaseModel.clear_session()
            try:
                session = BaseModel.get_session()
                if hasattr(session, 'remove'):
                    session.remove()
                    logger.debug("Scoped session removed at request end")
            except Exception as e:
                logger.debug(f"Failed to remove scoped session at request end: {e}")
