output "api_url" {
  description = "Endereço da API. Chame com POST em /ask."
  value       = "${aws_apigatewayv2_api.http.api_endpoint}/ask"
}

output "agent_url" {
  description = "Endereço do agente. Chame com POST em /agent."
  value       = "${aws_apigatewayv2_api.http.api_endpoint}/agent"
}

output "bucket" {
  description = "Bucket dos documentos (pasta docs/) e do índice."
  value       = data.aws_s3_bucket.docs.id
}

output "ingest_function" {
  description = "Função que lê docs/ e monta o índice."
  value       = aws_lambda_function.ingest.function_name
}

output "log_table" {
  description = "Tabela com o registro de cada consulta."
  value       = aws_dynamodb_table.log.name
}
