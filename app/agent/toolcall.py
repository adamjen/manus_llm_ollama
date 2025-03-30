import json
from typing import Any, List, Literal

from pydantic import Field

from app.agent.react import ReActAgent
from app.logger import logger
from app.prompt.toolcall import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import AgentState, Message, ToolCall
from app.tool import CreateChatCompletion, Terminate, ToolCollection


TOOL_CALL_REQUIRED = "Tool calls required but none provided"


class ToolCallAgent(ReActAgent):
    """Base agent class for handling tool/function calls with enhanced abstraction"""

    name: str = "toolcall"
    description: str = "an agent that can execute tool calls."

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    available_tools: ToolCollection = ToolCollection(
        CreateChatCompletion(), Terminate()
    )
    tool_choices: Literal["none", "auto", "required"] = "auto"
    special_tool_names: List[str] = Field(default_factory=lambda: [Terminate().name])

    tool_calls: List[ToolCall] = Field(default_factory=list)

    max_steps: int = 30

    async def think(self) -> bool:
        """Process current state and decide next actions using tools"""
        try:
            # Add next step prompt if exists
            if self.next_step_prompt:
                user_msg = Message.user_message(self.next_step_prompt)
                self.messages += [user_msg]

            # Get response with tool options - with enhanced error handling
            try:
                response = await self.llm.ask_tool(
                    messages=self.messages,
                    system_msgs=[Message.system_message(self.system_prompt)]
                    if self.system_prompt
                    else None,
                    tools=self.available_tools.to_params(),
                    tool_choice=self.tool_choices,
                )
            except Exception as e:
                logger.error(f"🚨 LLM communication error: {str(e)}")
                self.memory.add_message(
                    Message.assistant_message("I encountered an error processing your request")
                )
                return False

            # Safely handle tool calls extraction
            self.tool_calls = getattr(response, 'tool_calls', None) or []
            
            # Enhanced logging with null checks
            logger.info(f"✨ {self.name}'s thoughts: {getattr(response, 'content', 'No content')}")
            logger.info(
                f"🛠️ {self.name} selected {len(self.tool_calls)} tools to use"
            )
            
            if self.tool_calls:
                tool_names = []
                for call in self.tool_calls:
                    try:
                        tool_names.append(call.function.name)
                    except AttributeError:
                        tool_names.append("unnamed_tool")
                logger.info(f"🧰 Tools being prepared: {tool_names}")

            # Handle different tool_choice modes
            if self.tool_choices == "none":
                if self.tool_calls:
                    logger.warning(
                        f"🤔 Hmm, {self.name} tried to use tools when they weren't available!"
                    )
                if getattr(response, 'content', None):
                    self.memory.add_message(
                        Message.assistant_message(response.content)
                    )
                    return True
                return False

            # Create and add assistant message with null checks
            try:
                assistant_msg = (
                    Message.from_tool_calls(
                        content=getattr(response, 'content', ''),
                        tool_calls=self.tool_calls
                    )
                    if self.tool_calls
                    else Message.assistant_message(
                        getattr(response, 'content', 'No response generated')
                    )
                )
                self.memory.add_message(assistant_msg)
            except Exception as e:
                logger.error(f"📝 Message creation failed: {str(e)}")
                self.memory.add_message(
                    Message.assistant_message("I had trouble formulating my response")
                )
                return False

            # Decision logic
            if self.tool_choices == "required" and not self.tool_calls:
                logger.warning("🔧 Tools were required but none were provided")
                return True  # Will be handled in act()

            if self.tool_choices == "auto":
                has_content = bool(getattr(response, 'content', None))
                if not self.tool_calls and has_content:
                    return True
                elif not self.tool_calls and not has_content:
                    logger.warning("🤷 No tools or content generated in auto mode")
                    return False

            return bool(self.tool_calls)

        except Exception as e:
            logger.error(f"💥 Critical error in think(): {str(e)}", exc_info=True)
            self.memory.add_message(
                Message.assistant_message(
                    "I experienced a serious error while processing your request"
                )
            )
            return False

    async def act(self) -> str:
        """Execute tool calls and handle their results"""
        if not self.tool_calls:
            if self.tool_choices == "required":
                raise ValueError(TOOL_CALL_REQUIRED)

            # Return last message content if no tool calls
            return self.messages[-1].content or "No content or commands to execute"

        results = []
        for command in self.tool_calls:
            result = await self.execute_tool(command)
            logger.info(
                f"🎯 Tool '{command.function.name}' completed its mission! Result: {result}"
            )

            # Add tool response to memory
            tool_msg = Message.tool_message(
                content=result, tool_call_id=command.id, name=command.function.name
            )
            self.memory.add_message(tool_msg)
            results.append(result)

        return "\n\n".join(results)

    async def execute_tool(self, command: ToolCall) -> str:
        """Execute a single tool call with robust error handling"""
        if not command or not command.function or not command.function.name:
            return "Error: Invalid command format"

        name = command.function.name
        if name not in self.available_tools.tool_map:
            return f"Error: Unknown tool '{name}'"

        try:
            # Parse arguments
            args = json.loads(command.function.arguments or "{}")

            # Execute the tool
            logger.info(f"🔧 Activating tool: '{name}'...")
            result = await self.available_tools.execute(name=name, tool_input=args)

            # Format result for display
            observation = (
                f"Observed output of cmd `{name}` executed:\n{str(result)}"
                if result
                else f"Cmd `{name}` completed with no output"
            )

            # Handle special tools like `finish`
            await self._handle_special_tool(name=name, result=result)

            return observation
        except json.JSONDecodeError:
            error_msg = f"Error parsing arguments for {name}: Invalid JSON format"
            logger.error(
                f"📝 Oops! The arguments for '{name}' don't make sense - invalid JSON, arguments:{command.function.arguments}"
            )
            return f"Error: {error_msg}"
        except Exception as e:
            error_msg = f"⚠️ Tool '{name}' encountered a problem: {str(e)}"
            logger.error(error_msg)
            return f"Error: {error_msg}"

    async def _handle_special_tool(self, name: str, result: Any, **kwargs):
        """Handle special tool execution and state changes"""
        if not self._is_special_tool(name):
            return

        if self._should_finish_execution(name=name, result=result, **kwargs):
            # Set agent state to finished
            logger.info(f"🏁 Special tool '{name}' has completed the task!")
            self.state = AgentState.FINISHED

    @staticmethod
    def _should_finish_execution(**kwargs) -> bool:
        """Determine if tool execution should finish the agent"""
        return True

    def _is_special_tool(self, name: str) -> bool:
        """Check if tool name is in special tools list"""
        return name.lower() in [n.lower() for n in self.special_tool_names]
