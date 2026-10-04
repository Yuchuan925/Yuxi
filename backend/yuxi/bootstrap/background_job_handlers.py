"""后台作业 的领域处理器注册定义。"""

from __future__ import annotations

from yuxi.modules.background_jobs.registry import JobDefinition

JOB_DEFINITIONS = {
    definition.job_type: definition
    for definition in (
        JobDefinition(
            "knowledge_ingest",
            "yuxi.modules.knowledge.services.background_jobs",
            "run_knowledge_ingest",
            failure_function="fail_knowledge_file_job",
        ),
        JobDefinition(
            "knowledge_parse",
            "yuxi.modules.knowledge.services.background_jobs",
            "run_knowledge_parse",
            failure_function="fail_knowledge_file_job",
        ),
        JobDefinition(
            "knowledge_index",
            "yuxi.modules.knowledge.services.background_jobs",
            "run_knowledge_index",
            failure_function="fail_knowledge_file_job",
        ),
        JobDefinition(
            "knowledge_graph_index",
            "yuxi.modules.knowledge.services.background_jobs",
            "run_knowledge_graph",
        ),
        JobDefinition(
            "dataset_generation",
            "yuxi.modules.knowledge.evaluation.service",
            "run_dataset_generation_job",
            success_function="finish_dataset_generation_job",
            failure_function="fail_dataset_generation_job",
        ),
        JobDefinition(
            "rag_evaluation",
            "yuxi.modules.knowledge.evaluation.service",
            "run_rag_evaluation_job",
            success_function="finish_rag_evaluation_job",
            failure_function="fail_rag_evaluation_job",
        ),
    )
}
