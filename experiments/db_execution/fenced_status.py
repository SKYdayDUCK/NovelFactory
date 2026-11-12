"""Route only the database ablation through the same implementation as opt-in HTTP."""
from novel_factory.application.kernel import Kernel


class FencedStatusKernel(Kernel):
    get_task = Kernel.get_task_with_locked_work
