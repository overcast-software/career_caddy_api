from sqlalchemy import Column, Integer, Text, ForeignKey, String
from sqlalchemy.orm import relationship
from .base import BaseModel


class Resume(BaseModel):
    __tablename__ = "resume"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("user.id"))
    file_path = Column(String)
    title = Column(String)
    # Relationships
    user = relationship("User", back_populates="resumes")
    scores = relationship("Score", back_populates="resume")
    cover_letters = relationship("CoverLetter", back_populates="resume")
    applications = relationship("Application", back_populates="resume")
    summaries = relationship("Summary", back_populates="resume")
    resume_summaries = relationship("ResumeSummary", back_populates="resume")
    experiences = relationship(
        "Experience",
        secondary="resume_experience",
        back_populates="resumes",
    )
    certifications = relationship(
        "Certification",
        secondary="resume_certification",
        back_populates="resumes",
    )
    educations = relationship(
        "Education",
        secondary="resume_education",
        back_populates="resumes",
    )

    @classmethod
    def from_path_and_user_id(cls, path, user_id):
        with open(path) as file:
            body = file.read()
            resume, _ = cls.first_or_create(
                content=body, file_path=path, user_id=user_id
            )
        return resume

    def collated_content(self):
        # TODO body content from relationships
        pass
