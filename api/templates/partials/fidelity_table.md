| Fidelity | Means |
|---|---|
| `with-sources` | The original documents travel with the package. Every tuning operation is available after import, including re-chunking. |
| `chunks-only` | No original documents. The collection can be imported and queried, but it cannot be re-chunked, and re-embedding works from chunk text rather than the source, so it is approximate. |
