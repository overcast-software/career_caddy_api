from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel, Base


class Description(BaseModel):
    __tablename__ = "description"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)

    experiences = relationship(
        "Experience",
        secondary="experience_description",
        back_populates="descriptions",
        order_by=lambda: Base.metadata.tables["experience_description"].c.order,
    )
