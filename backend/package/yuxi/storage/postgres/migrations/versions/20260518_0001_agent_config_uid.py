"""Move agent configs from department scope to user scope.

Revision ID: 20260518_0001
Revises:
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260518_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_configs", sa.Column("uid", sa.String(), nullable=True))

    op.execute(
        """
        UPDATE agent_configs ac
        SET uid = u.uid
        FROM users u
        WHERE ac.uid IS NULL
          AND ac.created_by ~ '^[0-9]+$'
          AND u.id = ac.created_by::integer
        """
    )
    op.execute(
        """
        UPDATE agent_configs ac
        SET uid = u.uid
        FROM users u
        WHERE ac.uid IS NULL
          AND ac.created_by = u.uid
        """
    )
    op.execute(
        """
        UPDATE agent_configs ac
        SET uid = (
            SELECT u.uid
            FROM users u
            WHERE u.department_id = ac.department_id
              AND u.is_deleted = 0
            ORDER BY
              CASE WHEN u.role = 'superadmin' THEN 0 WHEN u.role = 'admin' THEN 1 ELSE 2 END,
              u.id ASC
            LIMIT 1
        )
        WHERE ac.uid IS NULL
        """
    )
    op.execute("DELETE FROM agent_configs WHERE uid IS NULL")

    op.execute("DROP INDEX IF EXISTS uq_agent_configs_department_agent_default")
    op.execute("DROP INDEX IF EXISTS ix_agent_configs_department_id")
    op.drop_constraint("uq_agent_configs_department_agent_name", "agent_configs", type_="unique")
    op.drop_constraint("agent_configs_department_id_fkey", "agent_configs", type_="foreignkey")
    op.drop_column("agent_configs", "department_id")

    op.alter_column("agent_configs", "uid", nullable=False)
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY uid, agent_id, name ORDER BY id) AS rn
            FROM agent_configs
        )
        UPDATE agent_configs ac
        SET name = LEFT(ac.name, 90) || '-' || ac.id::text
        FROM ranked
        WHERE ac.id = ranked.id AND ranked.rn > 1
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY uid, agent_id ORDER BY id) AS rn
            FROM agent_configs
            WHERE is_default IS TRUE
        )
        UPDATE agent_configs ac
        SET is_default = FALSE
        FROM ranked
        WHERE ac.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_foreign_key("agent_configs_uid_fkey", "agent_configs", "users", ["uid"], ["uid"])
    op.create_unique_constraint("uq_agent_configs_uid_agent_name", "agent_configs", ["uid", "agent_id", "name"])
    op.create_index("ix_agent_configs_uid", "agent_configs", ["uid"])
    op.create_index(
        "uq_agent_configs_uid_agent_default",
        "agent_configs",
        ["uid", "agent_id"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE"),
    )


def downgrade() -> None:
    op.add_column("agent_configs", sa.Column("department_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE agent_configs ac
        SET department_id = u.department_id
        FROM users u
        WHERE ac.uid = u.uid
        """
    )
    op.execute("DELETE FROM agent_configs WHERE department_id IS NULL")

    op.execute("DROP INDEX IF EXISTS uq_agent_configs_uid_agent_default")
    op.execute("DROP INDEX IF EXISTS ix_agent_configs_uid")
    op.drop_constraint("uq_agent_configs_uid_agent_name", "agent_configs", type_="unique")
    op.drop_constraint("agent_configs_uid_fkey", "agent_configs", type_="foreignkey")
    op.drop_column("agent_configs", "uid")

    op.alter_column("agent_configs", "department_id", nullable=False)
    op.create_foreign_key("agent_configs_department_id_fkey", "agent_configs", "departments", ["department_id"], ["id"])
    op.create_unique_constraint(
        "uq_agent_configs_department_agent_name",
        "agent_configs",
        ["department_id", "agent_id", "name"],
    )
    op.create_index("ix_agent_configs_department_id", "agent_configs", ["department_id"])
    op.create_index(
        "uq_agent_configs_department_agent_default",
        "agent_configs",
        ["department_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE"),
    )
