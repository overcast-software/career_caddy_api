from sqlalchemy import Column, Integer, Text, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class Summary(BaseModel):
    __tablename__ = "summary"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("user.id"))
    job_post_id = Column(Integer, ForeignKey("job_post.id", ondelete="CASCADE"))
    job_post = relationship("JobPost", back_populates="summaries")
    resumes = relationship(
        "Resume",
        secondary="resume_summaries",
        back_populates="summaries",
        overlaps="resume_summaries,resume",
    )  # many-to-many via resume_summaries
    user = relationship("User", back_populates="summaries")
    resume_summaries = relationship("ResumeSummaries", back_populates="summary")
