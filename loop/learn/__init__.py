"""Loop Learn loop — confirm/dismiss feedback and (task 6.2) threshold tuning.

The Learn phase closes Loop's autonomous cycle (design.md → "The five-agent
closed cycle"): the user's confirmations and dismissals teach Loop which loops
matter so surfacing improves over time (Requirement 5). Task 6.1 implements the
recording + dismiss + failure-handling half; task 6.2 layers bounded threshold
tuning on top via the seam left in :class:`loop.learn.feedback.LearnEngine`.
"""

from loop.learn.feedback import FeedbackResult, LearnEngine

__all__ = ["FeedbackResult", "LearnEngine"]
