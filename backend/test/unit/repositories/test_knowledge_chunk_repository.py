from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository, SQL_IN_BATCH_SIZE


def test_iter_batches_limits_sql_in_arguments():
    ids = [f"id-{index}" for index in range(SQL_IN_BATCH_SIZE * 2 + 1)]

    batches = list(KnowledgeChunkRepository._iter_batches(ids))

    assert [len(batch) for batch in batches] == [SQL_IN_BATCH_SIZE, SQL_IN_BATCH_SIZE, 1]
    assert [item for batch in batches for item in batch] == ids
