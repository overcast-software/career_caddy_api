from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel, Base


class Experience(BaseModel):
    __tablename__ = "experience"
    id = Column(Integer, primary_key=True, autoincrement=True)

    title = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)
    location = Column(String, nullable=True)
    company_id = Column(Integer, ForeignKey("company.id"), nullable=False)
    # Many-to-many with Resume via resume_experience
    resumes = relationship(
        "Resume",
        secondary="resume_experience",
        back_populates="experiences",
        overlaps="experience,resume",
    )
    company = relationship("Company")
    descriptions = relationship(
        "Description",
        secondary="experience_description",
        back_populates="experiences",
        order_by=lambda: Base.metadata.tables["experience_description"].c.order,
    )
