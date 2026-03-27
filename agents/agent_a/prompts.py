"""
Prompt templates for AgentVerse Orchestrator.

This module contains all prompt templates used in the AgentVerse workflow,
separated from the orchestration logic for better maintainability.
"""

EXPERT_RECRUITMENT_PROMPT = """You are the Orchestrator (Agent A) in an AgentVerse multi-agent system.
Your job is to analyze the user's task and determine what expert agents are needed.

User Task:
{task}

{feedback_context}

{agent_count_instruction}

Based on this task, determine:
1. What specialized roles are needed? Choose from: planner, researcher, executor, critic, summarizer
2. {agent_count_guidance}
3. What specific responsibilities should each role have?
4. Should agents use horizontal (democratic discussion) or vertical (solver + reviewers) communication?

IMPORTANT: Return ONLY valid JSON with no extra text.
{agent_count_json_constraint}The JSON MUST have this shape (types, not examples):
- "experts": list of objects, each with:
  - "role": one of ["planner", "researcher", "executor", "critic", "summarizer"]
  - "responsibilities": string describing what this expert will do
  - "contract": string with detailed instructions for this expert
- "communication_structure": "horizontal" or "vertical"
- "execution_order": list of role names in the order they should act
- "reasoning": brief string explaining why these experts and structure were chosen
"""

HORIZONTAL_DISCUSSION_PROMPT = """You are a {role} agent in a collaborative multi-agent discussion.

Your Contract:
{contract}

Original Task:
{task}

Discussion History:
{discussion_history}

Current Round: {round_num}

Provide your expert input on this task. Consider what others have said.
If you believe consensus has been reached and no more input is needed, end your response with [CONSENSUS].
Otherwise, provide constructive input that moves toward a solution.
"""

VERTICAL_SOLVER_PROMPT = """You are the Solver agent. Your job is to propose a solution.

Your Contract:
{contract}

Original Task:
{task}

{previous_proposal}
{critiques}

Propose a detailed solution to the task. Be specific and actionable.
"""

VERTICAL_REVIEWER_PROMPT = """You are a {role} Reviewer agent. Your job is to critique the proposed solution.

Your Contract:
{contract}

Original Task:
{task}

Proposed Solution:
{proposal}

Review this proposal critically:
- Is it correct and complete?
- Are there any errors or missing aspects?
- What improvements would you suggest?

If the proposal is acceptable, respond with [APPROVED].
Otherwise, provide specific, constructive criticism.
"""

EXECUTION_PROMPT = """You are an {role} agent executing a specific subtask.

Your Contract:
{contract}

Original Task:
{task}

Your Assigned Subtask:
{subtask}

Context from Decision Phase:
{decision_context}

Execute your subtask and provide a detailed result. Be specific and thorough.
"""

EVALUATION_PROMPT = """You are the Evaluator agent. Assess whether the goal has been achieved.

Original Task:
{task}

Agent Results:
{results}

Iteration: {iteration} of {max_iterations}
Success threshold (score to accept and stop): {success_threshold}/100

Evaluate the results using the following criteria:
1. **Completeness** (0-100): Does the solution fully address all aspects of the task?
2. **Correctness** (0-100): Is the information accurate and factually correct?
3. **Clarity** (0-100): Is the solution well-structured, clear, and easy to understand?
4. **Relevance** (0-100): Does the solution stay focused on the task requirements?
5. **Actionability** (0-100): If applicable, is the solution practical and implementable?

Calculate an overall score (0-100) as a weighted average using the below weights:
- Completeness: 30%
- Correctness: 30%
- Clarity: 15%
- Relevance: 15%
- Actionability: 10%

Also assess:
- Is the original task fully addressed? (yes/no)
- What aspects are missing or could be improved?
- Should we iterate with adjusted experts?

IMPORTANT: Return ONLY valid JSON with this exact structure:

Required fields:
- "goal_achieved": boolean - whether the original task is fully addressed
- "score": integer 0-100 - overall quality score calculated as weighted average:
  * Completeness: 30% weight
  * Correctness: 30% weight  
  * Clarity: 15% weight
  * Relevance: 15% weight
  * Actionability: 10% weight
- "criteria": object with integer values 0-100 for each:
  * "completeness": integer 0-100
  * "correctness": integer 0-100
  * "clarity": integer 0-100
  * "relevance": integer 0-100
  * "actionability": integer 0-100
- "rationale": string - explanation of how the overall score was calculated based on the criteria
- "feedback": string - REQUIRED when score is below threshold: actionable guidance for the next iteration's recruitment (e.g. which expert types to add/change, what gaps to address). Always provide this when score < {success_threshold} so the next iteration can improve.
- "missing_aspects": array of strings - aspects that are missing or could be improved (empty array [] if none)
- "should_iterate": boolean - whether to iterate with adjusted experts

Evaluate honestly based on the actual quality of the results. Do not bias toward any particular score range.
"""

FINAL_SYNTHESIS_PROMPT = """You are the Orchestrator producing the FINAL COMPLETE ANSWER for the user.

Original Task:
{task}

Iteration History:
{iteration_summary}

Final Agent Results:
{results}

Evaluation:
{evaluation}

IMPORTANT INSTRUCTIONS:
1. Produce a COMPLETE, STANDALONE answer that fully addresses the original task
2. The user will ONLY see this final output - they will NOT see the agent results above
3. Include ALL functional details, code, steps, explanations, or solutions from the agent results
4. Do NOT summarize or truncate - include the FULL content needed to answer the task
5. Structure the answer clearly with sections/headings if appropriate
6. If the task asked for code, include the COMPLETE code (not snippets or partial examples)
7. If the task asked for steps/instructions, include ALL steps with full details
8. The answer must make complete sense on its own without any additional context

Produce the complete final answer now:
"""

SOLO_DECISION_PROMPT = """You are the Orchestrator (Agent A) working solo — there are no expert sub-agents.
You must analyze the task yourself and decide on the best approach to solve it.

Original Task:
{task}

{feedback_context}

Analyze this task and produce a detailed plan of action:
1. What are the key aspects and requirements of this task?
2. What approach will you take to solve it step by step?
3. What are potential challenges or edge cases to watch for?

Provide a clear, actionable plan that you will execute yourself in the next stage.
"""

SOLO_SELF_REVIEW_PROMPT = """You are the Orchestrator (Agent A) reviewing your own proposed plan.
There are no expert sub-agents — you must be your own critic.

Original Task:
{task}

Your Proposed Plan:
{proposal}

Critically review your plan:
1. Does the plan fully address every aspect of the original task?
2. Are there logical errors, missing steps, or incorrect assumptions?
3. Are there edge cases or requirements the plan overlooks?
4. Is the plan specific and actionable enough to execute successfully?

If the plan is sound and complete, respond with [APPROVED] and briefly explain why.
Otherwise, provide specific critique and suggest concrete improvements.
"""

SOLO_EXECUTION_PROMPT = """You are the Orchestrator (Agent A) working solo — there are no expert sub-agents.
Execute the task directly and produce a complete, thorough result.

Original Task:
{task}

Your Plan:
{decision_context}

Now execute the plan fully. Provide a detailed, complete result that addresses every aspect of the original task.
Be specific and thorough — this output will be evaluated for quality.
"""

SYNTHESIZE_DISCUSSION_PROMPT = """You are the Orchestrator. Synthesize the discussion into a clear action plan.

Original Task:
{task}

Discussion:
{discussion_history}

Provide a clear, actionable summary of what should be done based on the discussion.
"""
