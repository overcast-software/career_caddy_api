# score is a rich join between a job description and a resume
# in addition to foriegn keys it has a score (1-100) and an explination (text)
from sqlalchemy import Column, Integer, ForeignKey, Text
from sqlalchemy.orm import relationship
from .base import BaseModel


class Score(BaseModel):
    __tablename__ = "score"
    id = Column(Integer, primary_key=True, autoincrement=True)
    score = Column(Integer, nullable=True)
    explanation = Column(Text, nullable=True)
    resume_id = Column(Integer, ForeignKey("resume.id"))
    job_post_id = Column(Integer)
    user_id = Column(Integer, ForeignKey("auth_user.id"))

    # Relationships
    resume = relationship("Resume", back_populates="scores")
    user = relationship("User", overlaps="scores")

    @property
    def company(self):
        """Get the company for this score through job_post"""
        if self.job_post_id:
            try:
                from job_hunting.lib.models.base import BaseModel
                from job_hunting.models import JobPost as DjangoJobPost, Company
                jp = DjangoJobPost.objects.filter(pk=self.job_post_id).first()
                if jp and jp.company_id:
                    return Company.objects.filter(pk=jp.company_id).first()
            except Exception:
                pass
        return None
