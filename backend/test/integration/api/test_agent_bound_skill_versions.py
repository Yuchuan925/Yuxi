"""专属 Skill 历史版本的真实 HTTP、数据库和文件证据。"""

import os
import asyncio
import re
import shutil
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.runtime_settings import get_skill_data_dir
from yuxi.modules.extensions.skills.models import Skill, SkillVersion
from yuxi.modules.extensions.skills.shared import get_skills_root_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(autouse=True)
async def close_projection_pool():
    """关闭当前测试事件循环的投影连接。"""
    yield
    from yuxi.infrastructure.postgres.manager import pg_manager

    await pg_manager.close()


@pytest_asyncio.fixture
async def bound_version_skill(test_client, admin_headers):
    """建立真实绑定并在退出时清理 Agent。"""
    slug = f"pytest-version-{uuid.uuid4().hex[:8]}"
    created = await test_client.post(
        "/api/agent",
        headers=admin_headers,
        json={
            "slug": slug,
            "name": slug,
            "visibility": "shared",
            "share_config": {"version": 2, "read_scope": None, "manage_scope": None},
        },
    )
    assert created.status_code == 200, created.text
    response = await test_client.post(f"/api/agent/{slug}/self-skill", headers=admin_headers)
    assert response.status_code == 200, response.text
    try:
        yield slug, response.json()["skill"]["slug"]
    finally:
        deleted = await test_client.delete(f"/api/agent/{slug}", headers=admin_headers)
        assert deleted.status_code in {200, 404}, deleted.text


async def current_content(slug):
    """独立读取持久引用，定位可回读的内容目录。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with async_sessionmaker(engine)() as db:
            item = await db.scalar(select(Skill).where(Skill.slug == slug))
            return get_skill_data_dir() / item.dir_path
    finally:
        await engine.dispose()


async def test_save_release_restore_delete_complete_package(test_client, admin_headers, bound_version_skill):
    """完整保存与历史引用同一内容，恢复后删除该历史仍保留当前包。"""
    _, slug = bound_version_skill
    base = f"/api/system/skills/{slug}"
    before = (await test_client.get(f"{base}/content", headers=admin_headers)).json()["data"]
    saved = await test_client.put(
        f"{base}/content",
        headers=admin_headers,
        json={
            "expected_revision": before["revision"],
            "release": True,
            "changes": [
                {"action": "write", "path": "SKILL.md", "content": before["files"]["SKILL.md"] + "\nV1 root"},
                {"action": "create", "path": "scripts/run.py", "content": "print('v1')\n"},
                {"action": "create", "path": "references/note.md", "content": "v1 reference"},
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    label = saved.json()["data"]["published_version"]["version"]
    assert re.fullmatch(r"\d{8}-[0-9a-f]{8}", label)
    v1 = await current_content(slug)
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with async_sessionmaker(engine)() as db:
            item = await db.scalar(select(Skill).where(Skill.slug == slug))
            row = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == item.id))
            assert row.dir_path == item.dir_path
            assert row.content_hash == item.content_hash
        changed = await test_client.put(
            f"{base}/content",
            headers=admin_headers,
            json={
                "expected_revision": saved.json()["data"]["revision"],
                "changes": [
                    {"action": "write", "path": "scripts/run.py", "content": "print('v2')\n"},
                    {"action": "create", "path": "extra.md", "content": "unreleased"},
                ],
            },
        )
        assert changed.status_code == 200, changed.text
        assert v1.joinpath("scripts/run.py").read_text() == "print('v1')\n"
        stale = await test_client.post(
            f"{base}/versions/{label}/restore",
            headers=admin_headers,
            json={"expected_revision": saved.json()["data"]["revision"]},
        )
        assert stale.status_code == 409, stale.text
        restored = await test_client.post(
            f"{base}/versions/{label}/restore",
            headers=admin_headers,
            json={"expected_revision": changed.json()["data"]["revision"]},
        )
        assert restored.status_code == 200, restored.text
        assert await current_content(slug) == v1
        assert v1.joinpath("references/note.md").read_text() == "v1 reference"
        assert not v1.joinpath("extra.md").exists()
        deleted = await test_client.delete(f"{base}/versions/{label}", headers=admin_headers)
        assert deleted.status_code == 200, deleted.text
        assert v1.joinpath("SKILL.md").read_text().endswith("V1 root")
        assert (await test_client.get(f"{base}/versions", headers=admin_headers)).json()["data"]["versions"] == []
        read = (await test_client.get(f"{base}/content", headers=admin_headers)).json()["data"]
        final = await test_client.put(
            f"{base}/content",
            headers=admin_headers,
            json={
                "expected_revision": read["revision"],
                "changes": [{"action": "write", "path": "SKILL.md", "content": read["files"]["SKILL.md"] + " updated"}],
            },
        )
        assert final.status_code == 200, final.text
        assert not v1.exists()
    finally:
        await engine.dispose()


async def test_save_release_commit_failure_keeps_old_reference(
    test_client, admin_headers, bound_version_skill, monkeypatch
):
    """真实 PG 提交失败，旧引用和字节保持有效，新包与历史均不留下。"""
    from yuxi.modules.extensions.skills.content import save_skill_content
    from yuxi.modules.identity.models import User

    _, slug = bound_version_skill
    before = (await test_client.get(f"/api/system/skills/{slug}/content", headers=admin_headers)).json()["data"]
    original = await current_content(slug)
    original_bytes = original.joinpath("SKILL.md").read_bytes()
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            user = await db.scalar(select(User).where(User.uid == me.json()["uid"]))

            async def fail_commit():
                """在已写入完整包后拒绝数据库提交。"""
                raise RuntimeError("injected commit failure")

            monkeypatch.setattr(db, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="commit failure"):
                await save_skill_content(
                    db,
                    slug=slug,
                    operator=user,
                    expected_revision=before["revision"],
                    changes=[
                        {"action": "write", "path": "SKILL.md", "content": before["files"]["SKILL.md"] + " changed"}
                    ],
                    release=True,
                )
        assert await current_content(slug) == original
        assert original.joinpath("SKILL.md").read_bytes() == original_bytes
        assert list(original.parent.iterdir()) == [original]
        async with factory() as db:
            item = await db.scalar(select(Skill).where(Skill.slug == slug))
            assert await db.scalar(select(SkillVersion.id).where(SkillVersion.skill_id == item.id)) is None
    finally:
        await engine.dispose()


async def test_reader_and_corrupt_history_cannot_restore(
    test_client, admin_headers, standard_user, bound_version_skill
):
    """读取权限不能管理版本，历史字节损坏不能覆盖有效当前包。"""
    agent, slug = bound_version_skill
    base = f"/api/system/skills/{slug}"
    grants = {
        "version": 2,
        "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
        "manage_scope": None,
    }
    response = await test_client.put(f"/api/agent/{agent}", headers=admin_headers, json={"share_config": grants})
    assert response.status_code == 200, response.text
    before = (await test_client.get(f"{base}/content", headers=admin_headers)).json()["data"]
    published = await test_client.put(
        f"{base}/content",
        headers=admin_headers,
        json={"expected_revision": before["revision"], "changes": [], "release": True},
    )
    assert published.status_code == 200, published.text
    label = published.json()["data"]["published_version"]["version"]
    history = await current_content(slug)
    revision = published.json()["data"]["revision"]
    for method, path, payload in [
        ("post", f"/versions/{label}/restore", {"expected_revision": revision}),
        ("put", "/content", {"expected_revision": revision, "changes": [], "release": True}),
    ]:
        denied = await getattr(test_client, method)(base + path, headers=standard_user["headers"], json=payload)
        assert denied.status_code in {400, 403, 404}, denied.text
    saved = await test_client.put(
        f"{base}/content",
        headers=admin_headers,
        json={
            "expected_revision": revision,
            "changes": [{"action": "write", "path": "SKILL.md", "content": before["files"]["SKILL.md"] + " current"}],
        },
    )
    assert saved.status_code == 200, saved.text
    current = await current_content(slug)
    history.joinpath("SKILL.md").write_text("corrupt")
    rejected = await test_client.post(
        f"{base}/versions/{label}/restore",
        headers=admin_headers,
        json={"expected_revision": saved.json()["data"]["revision"]},
    )
    assert rejected.status_code == 400 and "损坏" in rejected.text, rejected.text
    assert current.joinpath("SKILL.md").read_text().endswith(" current")


async def test_concurrent_content_save_only_one_commits(test_client, admin_headers, bound_version_skill):
    """同一个完整编辑基准仅允许一笔提交成功，不形成混合包。"""
    _, slug = bound_version_skill
    base = f"/api/system/skills/{slug}"
    before = (await test_client.get(f"{base}/content", headers=admin_headers)).json()["data"]
    results = await asyncio.gather(
        *[
            test_client.put(
                f"{base}/content",
                headers=admin_headers,
                json={
                    "expected_revision": before["revision"],
                    "changes": [
                        {"action": "write", "path": "SKILL.md", "content": before["files"]["SKILL.md"] + suffix}
                    ],
                },
            )
            for suffix in (" A", " B")
        ]
    )
    assert sorted(result.status_code for result in results) == [200, 409], [result.text for result in results]
    current = await current_content(slug)
    assert current.joinpath("SKILL.md").read_text().endswith((" A", " B"))
    assert list(current.parent.iterdir()) == [current]


async def test_content_save_does_not_refresh_all_users(test_client, admin_headers, bound_version_skill, monkeypatch):
    """普通内容变更保持现有投影，执行准备从当前持久引用刷新。"""
    from yuxi.modules.extensions.skills.content import save_skill_content
    from yuxi.modules.extensions.skills import projection
    from yuxi.modules.identity.models import User

    _, slug = bound_version_skill
    me = (await test_client.get("/api/auth/me", headers=admin_headers)).json()
    await projection.refresh_user_skill_projection_async(me["uid"])
    path = projection.get_user_skills_root_dir(me["uid"]) / slug / "SKILL.md"
    old = path.read_bytes()
    before = (await test_client.get(f"/api/system/skills/{slug}/content", headers=admin_headers)).json()["data"]
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = await db.scalar(select(User).where(User.uid == me["uid"]))

            async def forbidden(*args):
                """内容保存不得触发全用户投影用例。"""
                raise AssertionError("content save invoked global projection")

            with monkeypatch.context() as patch:
                patch.setattr(projection, "invalidate_skill_projections", forbidden)
                patch.setattr(projection, "refresh_skill_projections", forbidden)
                await save_skill_content(
                    db,
                    slug=slug,
                    operator=user,
                    expected_revision=before["revision"],
                    changes=[{"action": "write", "path": "SKILL.md", "content": before["files"]["SKILL.md"] + " next"}],
                )
        assert path.read_bytes() == old
        await projection.refresh_user_skill_projection_async(me["uid"])
        assert path.read_text().endswith(" next")
    finally:
        await engine.dispose()


async def test_restore_revalidates_dependencies_against_current_agent_scope(
    test_client, admin_headers, standard_user, bound_version_skill
):
    """latest 移除依赖后扩大 Agent 授权，旧快照不得恢复不覆盖新范围的依赖。"""
    agent_slug, slug = bound_version_skill
    base = f"/api/system/skills/{slug}"
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    dep_slug = f"pytest-version-dep-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dep_source = get_skills_root_dir() / dep_slug
    dep_source.mkdir()
    dep_source.joinpath("SKILL.md").write_text(f"---\nslug: {dep_slug}\nname: dependency\ndescription: test\n---\n")
    try:
        async with factory() as db:
            db.add(
                Skill(
                    slug=dep_slug,
                    name=dep_slug,
                    description="dependency",
                    source_type="upload",
                    dir_path=f"shared/{dep_slug}",
                    share_config={
                        "version": 2,
                        "read_scope": {"access_level": "user", "user_uids": [me.json()["uid"]], "department_ids": []},
                        "manage_scope": None,
                    },
                    created_by=me.json()["uid"],
                    enabled=True,
                )
            )
            await db.commit()
        saved = await test_client.put(
            f"{base}/content",
            headers=admin_headers,
            json={
                "changes": [{"action": "dependencies", "skill_dependencies": [dep_slug]}],
                "expected_revision": (await test_client.get(f"{base}/content", headers=admin_headers)).json()["data"][
                    "revision"
                ],
                "release": True,
            },
        )
        assert saved.status_code == 200, saved.text
        label = saved.json()["data"]["published_version"]["version"]
        removed = await test_client.put(
            f"{base}/dependencies",
            headers=admin_headers,
            json={
                "skill_dependencies": [],
                "expected_revision": (
                    await test_client.get(f"{base}/file?path=SKILL.md", headers=admin_headers)
                ).json()["data"]["revision"],
            },
        )
        assert removed.status_code == 200, removed.text
        grants = {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": [standard_user["user"]["uid"]], "department_ids": []},
            "manage_scope": None,
        }
        published = await test_client.put(
            f"/api/agent/{agent_slug}", headers=admin_headers, json={"share_config": grants}
        )
        assert published.status_code == 200, published.text
        listed = await test_client.get(f"{base}/versions", headers=admin_headers)
        source = (await current_content(slug)) / "SKILL.md"
        before = source.read_bytes()
        rejected = await test_client.post(
            f"{base}/versions/{label}/restore",
            headers=admin_headers,
            json={"expected_revision": listed.json()["data"]["revision"]},
        )
        assert rejected.status_code == 400, rejected.text
        assert dep_slug in rejected.text
        assert source.read_bytes() == before
        detail = await test_client.get(base, headers=admin_headers)
        assert detail.json()["data"]["skill_dependencies"] == []
    finally:
        async with factory() as db:
            dep = await db.scalar(select(Skill).where(Skill.slug == dep_slug))
            await db.delete(dep)
            await db.commit()
        shutil.rmtree(dep_source)
        await engine.dispose()


async def test_release_collision_preserves_existing_history(
    test_client, admin_headers, bound_version_skill, monkeypatch
):
    """相同版本标识被唯一约束拒绝，不删除已发布内容。"""
    from types import SimpleNamespace
    from yuxi.modules.extensions.skills import content
    from yuxi.modules.identity.models import User

    _, slug = bound_version_skill
    data = (await test_client.get(f"/api/system/skills/{slug}/content", headers=admin_headers)).json()["data"]
    me = (await test_client.get("/api/auth/me", headers=admin_headers)).json()
    path = await current_content(slug)
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(content.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
            for attempt in range(2):
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    user = await db.scalar(select(User).where(User.uid == me["uid"]))
                    if attempt == 0:
                        result = await content.save_skill_content(
                            db, slug=slug, operator=user, expected_revision=data["revision"], changes=[], release=True
                        )
                        label = result.published_version["version"]
                    else:
                        with pytest.raises(content.SkillEditConflict, match="标识冲突"):
                            await content.save_skill_content(
                                db,
                                slug=slug,
                                operator=user,
                                expected_revision=data["revision"],
                                changes=[],
                                release=True,
                            )
        async with async_sessionmaker(engine)() as db:
            item = await db.scalar(select(Skill).where(Skill.slug == slug))
            rows = list((await db.scalars(select(SkillVersion).where(SkillVersion.skill_id == item.id))).all())
            assert len(rows) == 1 and rows[0].version == label
        assert path.joinpath("SKILL.md").read_text() == data["files"]["SKILL.md"]
    finally:
        await engine.dispose()


async def test_committed_response_survives_following_save_prune(
    test_client, admin_headers, bound_version_skill, monkeypatch
):
    """暂停首笔提交后，让第二笔保存清理旧包，首笔仍准确返回成功。"""
    from yuxi.modules.extensions.skills import bound, content
    from yuxi.modules.identity.models import User
    from test.integration.api.test_agent_bound_skill import skill_zip

    agent, slug = bound_version_skill
    data = (await test_client.get(f"/api/system/skills/{slug}/content", headers=admin_headers)).json()["data"]
    me = (await test_client.get("/api/auth/me", headers=admin_headers)).json()
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    first_committed, second_finished = asyncio.Event(), asyncio.Event()
    first_path = None
    original_prune = content.prune_skill_content

    async def pause_first_prune(db_to_prune, value):
        """首笔已经提交但尚未返回，将调度权交给后续保存。"""
        nonlocal first_path
        if not first_committed.is_set():
            first_path = await current_content(slug)
            first_committed.set()
            await asyncio.wait_for(second_finished.wait(), timeout=20)
        await original_prune(db_to_prune, value)

    async def upload():
        """通过真实绑定用例写入首笔内容。"""
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = await db.scalar(select(User).where(User.uid == me["uid"]))
            return await bound.upload_agent_bound_skill(
                db,
                agent_slug=agent,
                operator=user,
                filename="skill.zip",
                file_bytes=skill_zip(),
                expected_revision=data["revision"],
            )

    async def save_following():
        """在首笔提交之后提交新根，并清理不再引用的首笔包。"""
        await asyncio.wait_for(first_committed.wait(), timeout=20)
        try:
            latest = (await test_client.get(f"/api/system/skills/{slug}/content", headers=admin_headers)).json()["data"]
            response = await test_client.put(
                f"/api/system/skills/{slug}/content",
                headers=admin_headers,
                json={
                    "expected_revision": latest["revision"],
                    "changes": [
                        {"action": "write", "path": "SKILL.md", "content": latest["files"]["SKILL.md"] + " following"}
                    ],
                },
            )
            assert response.status_code == 200, response.text
        finally:
            second_finished.set()

    try:
        # 第二笔由 API 独立进程执行，补丁只暂停首笔用例。
        with monkeypatch.context() as patch:
            patch.setattr(content, "prune_skill_content", pause_first_prune)
            result, _ = await asyncio.gather(upload(), save_following())
        assert result["skill"]["slug"] == slug
        assert result["revision"]
        assert not first_path.exists()
        assert (await current_content(slug)).joinpath("SKILL.md").read_text().endswith(" following")
    finally:
        await engine.dispose()
