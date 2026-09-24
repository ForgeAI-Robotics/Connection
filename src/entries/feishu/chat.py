"""Chat and ambiguous intent requests go to the same brain as tasks."""
class DeepSeekChat:
    def __init__(self, brain):
        self.brain = brain
    async def reply(self, text):
        return await self.brain.chat(text)
    async def route(self, text):
        return await self.brain.chat(text, route=True)
