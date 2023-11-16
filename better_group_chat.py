from typing import Dict, List, Optional, Union
from autogen import Agent, GroupChat, ConversableAgent, GroupChatManager
import logging
import sys
import pprint

from src.agent_prompts import (
    AGENT_PREFACE,
    AGENT_TEAM_INTRO,
    AGENT_DESCRIPTION_SUMMARIZER,
    DEFAULT_COVERSATION_MANAGER_SYSTEM_PROMPT,
    PERSONA_DISCUSSION_FOR_NEXT_STEP_SYSTEM_PROMPT,
    PERSONA_DISCUSSION_FOR_NEXT_STEP_PROMPT,
    EXTRACT_NEXT_ACTOR_FROM_DISCUSSION_PROMPT,
)
from src.utils.misc import light_llm4_wrapper, extract_json_response

logger = logging.getLogger(__name__)


class BetterGroupChat(GroupChat):
    def __init__(
        self,
        agents: List[Agent],
        messages: List[Dict] = [],
        max_round: int = 10,
        admin_name: str = "Admin",
        func_call_filter: bool = True,
        summarize_agent_descriptions: bool = False,
        persona_discussion: bool = False,
        inject_persona_discussion: bool = False,
    ):
        super().__init__(agents, messages, max_round, admin_name, func_call_filter)

        self.summarize_agent_descriptions = summarize_agent_descriptions
        self.persona_discussion = persona_discussion
        self.inject_persona_discussion = inject_persona_discussion
        self.agent_descriptions = []
        self.agent_team_description = ""
        for agent in agents:
            if self.summarize_agent_descriptions:
                description = light_llm4_wrapper(
                    AGENT_DESCRIPTION_SUMMARIZER.format(
                        agent_system_message=agent.system_message
                    )
                ).text
            else:
                description = agent.system_message
            self.agent_descriptions.append(
                {
                    "name": agent.name,
                    "description": description,
                    "llm_config": agent.llm_config,
                }
            )

        self.agent_team_list = [
            f"{'*' * 20}\nAGENT_NAME: {agent['name']}\nAGENT_DESCRIPTION: {agent['description']}\n{self.describe_agent_actions(agent)}{'*' * 20}\n"
            for agent in self.agent_descriptions
        ]

        agent_team_intro = AGENT_TEAM_INTRO.format(
            agent_team_list="\n".join(self.agent_team_list)
        )

        AGENT_PREFACE_WITH_TEAM = f"{AGENT_PREFACE}\n\n{agent_team_intro}"

        print("AGENT_PREFACE_WITH_TEAM: ", AGENT_PREFACE_WITH_TEAM)

        # Loop through each agent and prepend the AGENT_PREFACE to their system message
        for agent in agents:
            agent.update_system_message(
                f"{AGENT_PREFACE_WITH_TEAM}\n\n{agent.system_message}"
            )

    def describe_agent_actions(self, agent: ConversableAgent):
        callable_functions = agent["llm_config"].get("functions", False)

        print("CALLABLE_FUNCTIONS: ", callable_functions)

        if callable_functions:
            AGENT_FUNCTION_LIST = f"AGENT_REGISTERED_FUNCTIONS:"
            for function in callable_functions:
                AGENT_FUNCTION_LIST += f"""
----------------------------------------
FUNCTION_NAME: {function["name"]}
FUNCTION_DESCRIPTION: {function["description"]}
FUNCTION_ARGUMENTS: {function["parameters"]}
----------------------------------------\n"""
            return AGENT_FUNCTION_LIST

        return ""

    def select_speaker_msg(self, agents: List[Agent]):
        """Return the system message for selecting the next speaker."""
        agent_team = self._participant_roles()
        agent_names = [agent.name for agent in agents]

        if self.persona_discussion:
            return PERSONA_DISCUSSION_FOR_NEXT_STEP_SYSTEM_PROMPT
        else:
            return DEFAULT_COVERSATION_MANAGER_SYSTEM_PROMPT.format(
                agent_team=agent_team,
                agent_names=agent_names,
            )

    def _participant_roles(self):
        roles = []
        for agent in self.agent_descriptions:
            if agent["description"].strip() == "":
                logger.warning(
                    f"The agent '{agent['name']}' has an empty description, and may not work well with GroupChat."
                )
            roles.append(
                f"{'-' * 50}\n"
                + f"NAME: {agent['name']}\nDESCRIPTION: {agent['description']}"
                + f"\n{'-' * 50}"
            )
        return "\n".join(roles)

    def select_speaker(self, last_speaker: Agent, selector: ConversableAgent):
        """Select the next speaker."""
        if (
            self.func_call_filter
            and self.messages
            and "function_call" in self.messages[-1]
        ):
            # find agents with the right function_map which contains the function name
            agents = [
                agent
                for agent in self.agents
                if agent.can_execute_function(
                    self.messages[-1]["function_call"]["name"]
                )
            ]
            if len(agents) == 1:
                # only one agent can execute the function
                return agents[0]
            elif not agents:
                # find all the agents with function_map
                agents = [agent for agent in self.agents if agent.function_map]
                if len(agents) == 1:
                    return agents[0]
                elif not agents:
                    raise ValueError(
                        f"No agent can execute the function {self.messages[-1]['name']}. "
                        "Please check the function_map of the agents."
                    )
        else:
            agents = self.agents
            # Warn if GroupChat is underpopulated
            n_agents = len(agents)
            if n_agents < 3:
                logger.warning(
                    f"GroupChat is underpopulated with {n_agents} agents. Direct communication would be more efficient."
                )

        selector.update_system_message(self.select_speaker_msg(agents))

        print("SELECTOR: ", selector.name)
        # print("SELECTOR_SYSTEM_MESSAGE: ", selector.system_message)
        # print("MESSAGES: ", self.messages
        #     + [
        #         {
        #             "role": "system",
        #             "content": f"Read the above conversation. Then select the next agent from {[agent.name for agent in agents]} to speak. Only return the agent.",
        #         }
        #     ])

        get_next_actor_message = ""

        if self.persona_discussion:
            get_next_actor_content = PERSONA_DISCUSSION_FOR_NEXT_STEP_PROMPT.format(
                task_goal=self.messages[0]["content"],
                agent_team=self._participant_roles(),
                conversation_history=self.messages,
            )
        else:
            get_next_actor_content = f"Read the above conversation. Then select the next agent from {[agent.name for agent in agents]} to speak. Only return the JSON object with your 'analysis' and chosen 'next_actor'."

        get_next_actor_message = self.messages + [
            {
                "role": "system",
                "content": get_next_actor_content,
            }
        ]

        # print("GET NEXT ACTOR MESSAGE:", get_next_actor_message)

        final, response = selector.generate_oai_reply(get_next_actor_message)
        print("CHAT_MANAGER_RESPONSE:", response)
        if self.persona_discussion:
            if self.inject_persona_discussion:
                # Inject the persona discussion into the message history

                header = f"####\nSOURCE_AGENT: AGENT_COUNCIL\n####"
                response = f"{header}\n\n" + response
                self.messages.append({"role": "system", "content": response})
                # send the persona discussion to all agents
                for agent in self.agents:
                    selector.send(response, agent, request_reply=False, silent=True)

            extracted_next_actor = light_llm4_wrapper(
                EXTRACT_NEXT_ACTOR_FROM_DISCUSSION_PROMPT.format(
                    actor_options=[agent.name for agent in agents],
                    discussion=response,
                )
            )
            response_json = extract_json_response(extracted_next_actor)
            print("GET_NEXT_ACTOR_RESPONSE: \n", response_json)
            name = response_json["next_actor"]
        else:
            response_json = extract_json_response(response)
            name = response_json["next_actor"]
        print("FINAL: ", final)
        print("NAME: ", name)
        if not final:
            # i = self._random.randint(0, len(self._agent_names) - 1)  # randomly pick an id
            return self.next_agent(last_speaker, agents)
        try:
            return self.agent_by_name(name)
        except ValueError:
            logger.warning(
                f"GroupChat select_speaker failed to resolve the next speaker's name. Speaker selection will default to the UserProxy if it exists, otherwise we defer to next speaker in the list. This is because the speaker selection OAI call returned:\n{name}"
            )
            # Check if UserProxy exists in the agent list.
            for agent in agents:
                # Check for "User" or "UserProxy" in the agent name
                if agent.name == "User" or agent.name == "UserProxy":
                    return self.agent_by_name(agent.name)

            return self.next_agent(last_speaker, agents)


class BetterGroupChatManager(GroupChatManager):
    def __init__(
        self,
        groupchat: BetterGroupChat,
        name: Optional[str] = "chat_manager",
        # unlimited consecutive auto reply by default
        max_consecutive_auto_reply: Optional[int] = sys.maxsize,
        human_input_mode: Optional[str] = "NEVER",
        system_message: Optional[str] = "Group chat manager.",
        **kwargs,
    ):
        super().__init__(
            name=name,
            groupchat=groupchat,
            max_consecutive_auto_reply=max_consecutive_auto_reply,
            human_input_mode=human_input_mode,
            system_message=system_message,
            **kwargs,
        )
        # empty the self._reply_func_list
        self._reply_func_list = []
        self.register_reply(
            Agent,
            BetterGroupChatManager.run_chat,
            config=groupchat,
            reset_config=BetterGroupChat.reset,
        )
        # Allow async chat if initiated using a_initiate_chat
        self.register_reply(
            Agent,
            BetterGroupChatManager.a_run_chat,
            config=groupchat,
            reset_config=BetterGroupChat.reset,
        )

    def run_chat(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[BetterGroupChat] = None,
    ) -> Union[str, Dict, None]:
        """Run a group chat."""
        if messages is None:
            messages = self._oai_messages[sender]
        message = messages[-1]
        speaker = sender
        groupchat = config
        print("RUN_CHAT")
        for i in range(groupchat.max_round):
            # set the name to speaker's name if the role is not function
            if message["role"] != "function":
                message["name"] = speaker.name
            # add the agent header to the message
            groupchat.messages.append(message)
            # print("APPENDED_MESSAGE: ", message)
            print("SENDING_MESSAGE_FROM_SPEAKER: ", speaker.name)
            # print("GROUPCHAT_MESSAGES:\n")
            # pprint.pprint(groupchat.messages)
            # broadcast the message to all agents except the speaker
            for agent in groupchat.agents:
                if agent != speaker:
                    self.send(message, agent, request_reply=False, silent=True)
            if i == groupchat.max_round - 1:
                # the last round
                break
            try:
                # select the next speaker
                speaker = groupchat.select_speaker(speaker, self)
                print("NEXT_SPEAKER: ", speaker.name)
                # let the speaker speak
                reply = speaker.generate_reply(sender=self)
            except KeyboardInterrupt:
                # let the admin agent speak if interrupted
                if groupchat.admin_name in groupchat.agent_names:
                    # admin agent is one of the participants
                    speaker = groupchat.agent_by_name(groupchat.admin_name)
                    reply = speaker.generate_reply(sender=self)
                else:
                    # admin agent is not found in the participants
                    raise
            if reply is None:
                break
            header = f"####\nSOURCE_AGENT: {speaker.name}\n####"
            # check if reply is a string
            if header not in reply and isinstance(reply, str):
                reply = f"{header}\n\n" + reply
            # print("SENDING_REPLY:\n", reply)
            # The speaker sends the message without requesting a reply
            speaker.send(reply, self, request_reply=False)
            message = self.last_message(speaker)
        return True, None