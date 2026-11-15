variable "region" {
  description = "Região do laboratório (o Learner Lab costuma liberar us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefixo dos nomes dos recursos."
  type        = string
  default     = "rag-lab"
}

variable "gemini_api_key" {
  description = "Chave do Google AI Studio. Passe por TF_VAR_gemini_api_key, nunca em arquivo."
  type        = string
  sensitive   = true
}

variable "gemini_model" {
  description = "Modelo de geração. Os nomes mudam: confira no Google AI Studio."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "embed_model" {
  description = "Modelo de embeddings."
  type        = string
  default     = "gemini-embedding-001"
}

variable "top_k" {
  description = "Quantos trechos entram no prompt."
  type        = number
  default     = 4
}

variable "agent_max_steps" {
  description = "Rodadas de busca que o agente (POST /agent) pode fazer antes de responder. Cada rodada reenvia o contexto ao modelo."
  type        = number
  default     = 4
}

variable "max_output_tokens" {
  description = "Teto de tokens da resposta (guardrail de custo)."
  type        = number
  default     = 512
}
