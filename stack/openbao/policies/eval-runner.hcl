# The eval runner (and the agent it drives) needs: the model API key and a Langfuse API key to write
# datasets and scores. Nothing else - in particular it cannot read Langfuse's database passwords.
# (The agent's login to the inventory API stays in the lakehouse project's own OpenBao.)
path "secret/data/evals/anthropic" { capabilities = ["read"] }
path "secret/data/langfuse/api"    { capabilities = ["read"] }
