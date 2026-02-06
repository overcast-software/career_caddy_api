import secrets
import hashlib
from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from .base import BaseModel


class ApiKey(BaseModel):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)
    key_prefix = Column(String(16), nullable=False)
    user_id = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    scopes = Column(Text, nullable=True)  # JSON array of scopes
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @classmethod
    def generate_key(cls, name: str, user_id: int, expires_days: int = None, scopes: list = None):
        """Generate a new API key"""
        # Generate a secure random key
        key = f"jh_{secrets.token_urlsafe(32)}"
        
        # Create hash for storage
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        
        # Extract prefix for identification
        key_prefix = key[:12]
        
        # Set expiration
        expires_at = None
        if expires_days:
            expires_at = datetime.utcnow() + timedelta(days=expires_days)
        
        # Convert scopes to JSON string
        scopes_json = None
        if scopes:
            import json
            scopes_json = json.dumps(scopes)
        
        # Create the API key record
        api_key = cls(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=user_id,
            expires_at=expires_at,
            scopes=scopes_json
        )
        api_key.save()
        
        # Return the plain key (only time it's available)
        return api_key, key

    @classmethod
    def authenticate(cls, key: str):
        """Authenticate an API key and return the associated user_id"""
        if not key or not key.startswith("jh_"):
            return None
        
        # Hash the provided key
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        
        # Find the API key
        session = cls.get_session()
        api_key = session.query(cls).filter_by(key_hash=key_hash, is_active=True).first()
        
        if not api_key:
            return None
        
        # Check if expired
        if api_key.expires_at and api_key.expires_at < datetime.utcnow():
            return None
        
        # Update last used timestamp
        api_key.last_used_at = datetime.utcnow()
        session.add(api_key)
        session.commit()
        
        return api_key

    def get_scopes(self):
        """Get the scopes as a list"""
        if not self.scopes:
            return []
        
        import json
        try:
            return json.loads(self.scopes)
        except (json.JSONDecodeError, TypeError):
            return []

    def has_scope(self, scope: str):
        """Check if the API key has a specific scope"""
        scopes = self.get_scopes()
        return scope in scopes or "*" in scopes

    def revoke(self):
        """Revoke the API key"""
        self.is_active = False
        self.save()

    def to_dict(self):
        """Convert to dictionary (without sensitive data)"""
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "user_id": self.user_id,
            "is_active": self.is_active,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": self.get_scopes(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
