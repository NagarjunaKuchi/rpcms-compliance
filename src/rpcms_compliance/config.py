from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from RPCMS_COMPLIANCE_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RPCMS_COMPLIANCE_", env_file=".env", extra="ignore")

    jwt_key: str = Field(min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_audience: str | None = None
    database_url: str = "sqlite:///./rpcms_compliance.db"
    # True (default, dev/tests): create tables at startup. Set False in production and run `alembic upgrade head`.
    create_tables: bool = True
    # Role (string in the JWT 'roles' claim) that may act for any partner and requeue FAILED submissions.
    # Other tokens must carry a partner_id claim and can only act for that partner. Claim names are an assumption.
    admin_role: str = "compliance-admin"

    # Dotted paths to the clients. Defaults are development mocks that talk to nothing.
    ursb_client_impl: str = "rpcms_compliance.upstream.MockRegistryClient"
    ngo_client_impl: str = "rpcms_compliance.upstream.MockRegistryClient"
    amp_client_impl: str = "rpcms_compliance.upstream.MockAmpClient"
    nimes_client_impl: str = "rpcms_compliance.upstream.MockNimesClient"

    max_attempts: int = 8
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 3600
    worker_batch_size: int = 50
    worker_poll_seconds: float = 10.0
