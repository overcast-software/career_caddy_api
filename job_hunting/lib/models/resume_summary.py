from sqlalchemy import Column, Integer, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeSummaries(BaseModel):
    __tablename__ = "resume_summaries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(
        Integer, ForeignKey("resume.id", ondelete="CASCADE"), nullable=False
    )
    summary_id = Column(
        Integer, ForeignKey("summary.id", ondelete="CASCADE"), nullable=False
    )
    active = Column(Boolean)

    # Relationships
    resume = relationship("Resume", back_populates="resume_summaries", overlaps="summaries")
    summary = relationship("Summary", back_populates="resume_summaries", overlaps="resumes")

    __mapper_args__ = {"confirm_deleted_rows": False}

    @classmethod
    def ensure_single_active_for_resume(cls, resume_id, session=None):
        session = session or cls.get_session()
        try:
            rid = int(resume_id)
        except (TypeError, ValueError):
            return
        links = session.query(cls).filter_by(resume_id=rid).all()
        if not links:
            return
        actives = [l for l in links if bool(getattr(l, "active", False))]
        if len(actives) == 1:
            return
        if len(actives) == 0:
            keep_id = max(l.id for l in links)
        else:
            keep_id = max(l.id for l in actives)
        session.query(cls).filter_by(resume_id=rid).update(
            {cls.active: False}, synchronize_session=False
        )
        session.query(cls).filter_by(id=keep_id).update(
            {cls.active: True}, synchronize_session=False
        )
