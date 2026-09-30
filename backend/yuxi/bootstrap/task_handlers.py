"""迁移后的职责模块。"""

from __future__ import annotations


from yuxi.modules.tasks.registry import TaskDefinition

_TASK_DEFINITIONS = {
    definition.task_type: definition
    for definition in (
        TaskDefinition(
            "knowledge_ingest",
            "yuxi.modules.knowledge.services.tasks",
            "run_knowledge_ingest",
            failure_function="fail_knowledge_file_task",
        ),
        TaskDefinition(
            "knowledge_parse",
            "yuxi.modules.knowledge.services.tasks",
            "run_knowledge_parse",
            failure_function="fail_knowledge_file_task",
        ),
        TaskDefinition(
            "knowledge_index",
            "yuxi.modules.knowledge.services.tasks",
            "run_knowledge_index",
            failure_function="fail_knowledge_file_task",
        ),
        TaskDefinition(
            "knowledge_graph_index",
            "yuxi.modules.knowledge.services.tasks",
            "run_knowledge_graph",
        ),
        TaskDefinition(
            "dataset_generation",
            "yuxi.modules.knowledge.evaluation.service",
            "run_dataset_generation_task",
            success_function="finish_dataset_generation_task",
            failure_function="fail_dataset_generation_task",
        ),
        TaskDefinition(
            "rag_evaluation",
            "yuxi.modules.knowledge.evaluation.service",
            "run_rag_evaluation_task",
            success_function="finish_rag_evaluation_task",
            failure_function="fail_rag_evaluation_task",
        ),
    )
}
