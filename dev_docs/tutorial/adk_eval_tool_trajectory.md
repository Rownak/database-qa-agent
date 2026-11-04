# 2.Make Agent Eval-ready 

Tuesday, November 03, 2026 12:22 PM 

ADK project structure and agnet 

- You have written an agent 

   - It has tools 

   - Bulletproof SOP 

   - Gemini 

   - Wrote ADK eval to test 

- CLI says no agent found 

   - The directory structure is wrong 

   - Organizing files is paramount when building an agent 

   - Evaluation, Debugging and Deployment depends on directory strucute 

#### Structure: 

   - **Every agent lives in its own directory** 

   - **Directory must be python package** 

   - Package registration = '__init__.py' 

   - Agent Definition = agent.py 

   - Execution script = run_agent.py 

- __init__.py 

   - From . Import agent 

   - Transform this directory into a python package that ADK's CLI toolchain can load 

#### ADK Web and CLI 

- ADK Web: 

   - launches local dev dashboard 

   - chat interface and trace viewer 

   - interact with your agent and inspect behavior visually 

- ADK CLI 

   - runs agent against predefined test cases 

   - reports pass or fail scores 

   - autmated evaluation engine 

- Commands must be run from the parent directory 

- ADK scans for 

   - packages 

      - Locates init.py 

   - Looks for variable name **root_agent** 

- Components 

   - Agent.py 

      - Root_agent variable 

   - Run_agent.py 

      - Separate demo and testing logic from agent def 

   - Mock_data.josn 

      - Product catalog with items 

      - Mock data for **determinism** 

adk_agent_evaluation Page 1 

- **All Scenarios like** 


      - Budget freeze 

      - Restricted item 

   - Policy violation 

- Evaluation require reproducibility 

#### **Evaluations** 

- 

   - Baseline behavioral test 

   - Verify simplest behavior before adding complexity 

   - Testing blank function before writing logic 

   - 

- 

- Evaluate agent politely admit its limit 

- 

   - Test it with agent with no tool 

- It must pass before tool binding schema enforcement or evaluation work 

- It prooves the agent does not fabricate capabilities it does not process 

Schema: 

- 

- 

- Schemas are not suggestions but specifications 

- Schemas are specifications that make evaluation deteministic 

## Binding tools to the agent 

- 

- Tool 

- 

   - Plain python function with argument, return value 

- **DocString** describe its purpose 

   - 

   - 

      - Tools purpose and invocation contract 

      - Gemini use this text when to call this tool and what arg to pass 

   - 

- ADK perform auto schema introspection 

   - Inspect signature 

   - Read type hints 

   - 

      - Doc string 

   - Generate json tool declaration gemini require for strucutred function calling 

- Docstring not precise = model will make poor tool selections 

   - **First line of defense for trajectory correctness** 

- 

   - **Inflection Point** 

   - Point at which agent behavior becomes formally evaluable 

- 

   - **Reality** 

   - User don't type: _"I need a 16-inch pro laptop for the engineering department"_ 

- 

- 

   - _User type: ENG DEPT._ 

- 

- We need **enforceable contract** 

   - Allows difference between a **lookup failure vs Formatting error** 

- 

#### **Evaluation** 

- 

   - **Build on rock by enforcing strict contracts at the tool boundary** 

   - **Every input is validated before the tool ever executes** 

- 


## Schema as a contract: Pydantic enforcement 

   - Without schema as contract 

   - Your evaluation pipeline is build on top of a behavior that is non-deterministic 

   - A different prompt, different model version, slightly different phrasing 

      - Tool get laptopPro16 as raw arg 

      - Buisiness logic return not found 

   - This is the gap between it usually work vs garanteed to work 

- Pydantic close this gap 

### Pydantic 

   - Pedantic by design 

   - Schema as python class 

- Enforces every type, constraint, and format rule before code touches the data 

- 

   - Use cases: 

   - FastAPI – validates HTTP request body through pydantic model 

   - Data Engineering teams use it to enforce schemas in Spark and ETL pipelines 

   - LangChain uses it for structured LLM output parsing 

   - Tool Argument contracts at the agent boundary 

      - Ensure model can not pass malformed data to business logic 

   - 

- Pydantic models one per tool 


```python
class CatalogRequest(BaseModel): 

    item_id: str = Field( 

        ...,  

        description="The exact corporate identifier of the item.", 

        pattern=r'^[a-z0-9_]+$' # Enforce snake_case string, no spaces or capitals 

    ) 

 
 

class BudgetRequest(BaseModel): 

    department: Literal["engineering", "sales", "marketing", "hr"] = Field( 

        ...,  

        description="The strict requesting department enum value." 

    ) 
```

   - Inheret from base model 

   - Declares type field with constraints 

   - Explicit machine-readable specification at every tool boundary 

- Argument correctness 

   - Evaluate whether model passed the right values to tools 

### Schema gate pattern 

- 

   - Every tool pattern wraps its logic in a try except block 

- Call model validate 

- 

   - Input matches schema – then execution proceeds 

- If not – pydantic raises validation error 

   - Tool return strucuted error – no crash 

- 

- Self healing loop 

- Trigger by validation error – observation 

- Correction cycle 

   - Observe error 

   - Reason about constraint 

adk_agent_evaluation Page 3 

- Retry with corrected args 

▪ 

- Contract-driven agent development 

```python
root_agent = Agent( 

    name="pydantic_root_agent", 

    description="A procurement agent strictly adhering to Pydantic tool schemas.", 

    model="gemini-3-flash-preview", 

    instruction=( 

        "You are an Enterprise Procurement Agent.\n\n" 

        "STANDARD OPERATING PROCEDURE (SOP):\n" 

        "1. Catalog Validation: Always call `check_catalog` to verify the item exists, determine its price, and evaluate if it is restricted.\n" 

        "2. Budget Validation: Always call `check_budget` to ensure the requesting department has sufficient `remaining_budget` and is authorized for the item's `category`.\n" 

        "3. Rejection Path: If the item is restricted, the budget is insufficient, or the category is unauthorized, you MUST immediately call `refuse_request`.\n" 

        "4. Execution Path: ONLY if all preceding checks pass and the item is fully compliant may you call `place_order`.\n\n" 

        "AUTO-CORRECTION PROTOCOL:\n" 

        "If you receive a 'Schema Validation Error', do NOT immediately refuse the request. " 

        "Instead, carefully read the regex or enum rule in the error, auto-correct the human's dirty input " 

        "(e.g. converting 'Laptop Pro 16' to 'laptop_pro_16' or 'Eng' to 'engineering'), and retry the tool." 

    ), 

    tools=[check_catalog, check_budget, place_order, refuse_request] 

) 
```

Run_agent.py 

USER: >>hey can you order me a pro lptp 16 for the eng dept? thx 

[TOOL CALL]    check_catalog({'req': {'item_id': 'pro_lptp_16'}}) [OBSERVATION]  check_catalog -> {'error': "Item 'pro_lptp_16' not found in corporate catalog."} 

[TOOL CALL]    check_catalog({'req': {'item_id': 'laptop_pro_16'}}) 

- Summary 

   - Every correction attempt appears as a distinct event in the trajectory 

   - Evaluate both trajectory sequence correctness and argument correctness 

   - Schema are specification that make evaluation deterministic 

Scale up tool execution eval – ADK Eval 

- 

   - Automated reproducible regressiont test 

- tool_recall.test.json 

   - Agent equivalent of pytest fixture or an integration test 

   - Defines 

      - Input 

      - Expected behavior 

   - Success crieteria in single structured file 

- 

   - Golden trajectory 

   - Known correct trajectory – every future agent run will be compared against 

- Structure of tool_recall.test.json 

   - eval_set_id – human readable identifier 

adk_agent_evaluation Page 4 

   - List of eval_cases 

      - User content – the prompt sent to the agent 

   - Intermediate_data.tool_uses 

      - Golden trajectory 

      - Exact sequence of tool calls with their expected arg values 

      - 

- Each eval case should test one specific behavioral concern 

   - Concern: Does the agent follow the correct SOP for a valid procurement request? 

- Pass baselines test with regard to golden trajectory 

   - Add edge cases, adversarial inputs, and failure conditions 

- test_config.json 

   - Evaluation configuration 
```json
{ "criteria": { "tool_trajectory_avg_score": 1.0, "response_match_score": 0.0 } } 
```

   - Same tool, same order, same arg 

   - Response_match = 0, explicitly disable text based output comparison 

- Regression test 

   - Every time you change the agent's instruction, tools, model version 

      - Rerun this eval to verify nothing broke 

   - CLI command – you can embed it in ci/cd pipelines 

- Detail ○ Every ADK eval runs 

      - Writes full results 

      - Scores, trajectories metric details 

   - .ADK/eval_history folder ▪ Timestamp json files with full result 

      - Scorecard we build will parse these files 

   - This confied evaluation, evalset , test-config, CLI is architectural prerequisite 


Sample Codes: 



""" 
02_04 -- First Eval: Did the Agent Call the Right Tool? 

Introduces ADK evaluations to deterministically verify if the agent correctly triggered the expected tool chain when asked, instead of reading fluctuating text outputs. """ 

tool_recall.test.json 
```json

{ 

    "eval_set_id": "tool_recall_tests", 

    "name": "Procurement Tool Trajectory Eval", 

    "description": "Validates that the agent triggers the core SOP pathway sequentially, including self-correction.", 

    "eval_cases": [ 

        { 

            "eval_id": "test_standard_order_path", 

            "conversation": [ 

                { 

                    "invocation_id": "inv_001", 

                    "user_content": { 

                        "parts": [ 

                            { 

                                "text": "I need a 16-inch pro laptop for the engineering department." 

                            } 

                        ], 

                        "role": "user" 

                    }, 

                    "final_response": { 

                        "parts": [ 

                            { 

                                "text": "" 

                            } 

                        ], 

                        "role": "model" 

                    }, 

                    "intermediate_data": { 

                        "intermediate_responses": [], 

                        "tool_uses": [ 

                            { 

                                "name": "check_catalog", 

                                "args": { 

                                    "req": { 

                                        "item_id": "laptop_pro_16" 

                                    } 

                                } 

                            }, 

                            { 

                                "name": "check_budget", 

                                "args": { 

                                    "req": { 

                                        "department": "engineering" 

                                    } 

                                } 

                            }, 

                            { 

                                "name": "place_order", 

                                "args": { 

                                    "req": { 

                                        "item_id": "laptop_pro_16", 

                                        "department": "engineering", 

                                        "price": 2499.0 

                                    } 

                                } 

                            } 

                        ] 

                    } 

                } 

            ], 

            "session_input": { 

                "app_name": "procurement_agent", 

                "user_id": "evaluator", 

                "state": {} 

            } 

        } 

    ] 

} 
```