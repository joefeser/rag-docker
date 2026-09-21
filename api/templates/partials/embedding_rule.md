Vectors are only meaningful to the model that produced them. A vector from a
different embedding model is not merely different — it is meaningless in this
vector space, and a collection built from mismatched vectors answers every query
confidently and wrongly.

Import therefore **refuses** a package whose embedding model is not the one this
instance runs, rather than warning about it. This instance uses
`@@EMBED_MODEL@@` at @@EMBED_DIMENSIONS@@ dimensions.

Check any machine with `docker compose exec ollama ollama list`.
