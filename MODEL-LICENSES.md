# Model licences

fleet-tasks never ships model weights: each install pulls its own with
`ollama pull`. These are the licences of the local models in `catalog.json`.
Check a model's licence before pulling one that isn't listed (`ollama show
MODEL --license`). A test keeps every local model in `catalog.json` listed
here.

| Model | Licence | Commercial use | Notes |
|---|---|---|---|
| qwen3.6 (27b) | Apache-2.0 | yes | |
| qwen2.5-coder (7b, 14b) | Apache-2.0 | yes | The 3b size is under the Qwen Research Licence (non-commercial): don't use it for shared work |
| mistral-small3.2 (24b) | Apache-2.0 | yes | |
| mistral-nemo (12b) | Apache-2.0 | yes | |
| gemma3 (12b, 27b) | Gemma Terms of Use + Prohibited Use Policy | yes, with conditions | Redistributing the model passes its use restrictions on |
| llama3.1 (8b) | Llama 3.1 Community Licence | yes, under 700M monthly users | Shipping the model needs "Built with Llama" and the licence file |
| llama3.2 (3b) | Llama 3.2 Community Licence | yes, under 700M monthly users | As Llama 3.1 |
| llava (13b) | Llama 2 Community Licence (base) + LLaVA notice | with conditions | Also follow the base model's licence and OpenAI's terms for its training data |
| nomic-embed-text | Apache-2.0 | yes | Used for memory search by meaning |

Paid AI (Claude, Gemini, Copilot) runs through each vendor's own command-line
tool and account, under that vendor's terms. All of them are off in
`policy.example.json`.
