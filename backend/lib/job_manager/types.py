from enum import Enum


class JobQueue(Enum):
    MAIN_TASK_QUEUE = "main_task_queue"


class JobType(str, Enum):
    PHOTOBOOK_GENERATION = "photobook_generation"
