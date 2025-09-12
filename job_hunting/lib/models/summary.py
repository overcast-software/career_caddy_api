from sqlalchemy import Column, Integer, Text, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class Summary(BaseModel):
    __tablename__ = "summary"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("user.id"))
    job_post_id = Column(Integer, ForeignKey("job_post.id"))
    resume_id = Column(Integer, ForeignKey("resume.id"))

    job_post = relationship("JobPost", back_populates="summaries")
    resume = relationship("Resume", back_populates="summaries")
    user = relationship("User", back_populates="summaries")
