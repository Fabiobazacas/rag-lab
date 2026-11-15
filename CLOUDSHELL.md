# Passo a passo no AWS Cloud Shell

Roteiro completo para rodar o laboratório inteiro dentro do AWS Cloud Shell, sem instalar nada no seu computador. Para entender o que cada parte faz, veja o [README](README.md).

> Este roteiro ainda não foi ensaiado dentro do Cloud Shell. Foi validado no Windows. Se algum comando falhar, veja "Problemas comuns" no final.

## Antes de começar

1. Inicie o laboratório no AWS Academy (**Start Lab**) e espere o círculo ficar verde.
2. Clique em **AWS** para abrir o console. Confira que a região é **N. Virginia (us-east-1)**.
3. Tenha em mãos uma chave do Google AI Studio (https://aistudio.google.com), criada por você. Não compartilhe a chave nem a cole em arquivos, chats ou no repositório.
4. Abra o Cloud Shell: ícone `>_` na barra superior do console.

No Cloud Shell as credenciais da AWS já vêm na sessão. Você não precisa colar `AWS_ACCESS_KEY_ID` nem os outros.

## Parte 1: preparar o ambiente

### 1.1 Baixar o projeto

```bash
cd ~
git clone https://github.com/Fabiobazacas/rag-lab.git
cd rag-lab
```

Se a pasta já existir de uma sessão anterior: `cd ~/rag-lab && git pull`.

### 1.2 Instalar o Terraform

O Cloud Shell não traz o Terraform. Este bloco instala o binário em `~/bin`, sem precisar de root.

```bash
TFV=1.16.4
case "$(uname -m)" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
esac
mkdir -p ~/bin
curl -sL -o /tmp/terraform.zip "https://releases.hashicorp.com/terraform/$TFV/terraform_${TFV}_linux_${ARCH}.zip"
unzip -qo /tmp/terraform.zip -d ~/bin
export PATH="$HOME/bin:$PATH"
terraform version
```

Esperado: `Terraform v1.16.4`.

### 1.3 Manter o provedor fora do `$HOME`

O provedor AWS do Terraform ocupa cerca de 700 MB, e o Cloud Shell guarda só 1 GB em `$HOME`. Estas variáveis mandam o download para o `/tmp`, que é apagado quando a sessão termina (o `terraform init` baixa de novo).

```bash
export TF_DATA_DIR=/tmp/tfdata
export TF_PLUGIN_CACHE_DIR=/tmp/tfcache
mkdir -p "$TF_PLUGIN_CACHE_DIR"
```

### 1.4 Região e chave do Gemini

```bash
export AWS_DEFAULT_REGION=us-east-1

# Troque <chave_api> pela sua chave, mantendo as aspas simples.
export TF_VAR_gemini_api_key='<chave_api>'
```

O valor da variável deve ser só a chave. Para conferir sem mostrá-la, veja o tamanho com `echo ${#TF_VAR_gemini_api_key}` e compare com o tamanho da chave copiada. Se o número for 22 maior (o tamanho de `TF_VAR_gemini_api_key=`), o nome da variável foi colado junto com a chave: refaça o comando.

A chave fica no histórico do terminal desta sessão. Não copie esse histórico para lugar nenhum e revogue a chave no AI Studio depois da aula.

### 1.5 Conferir as credenciais

```bash
aws sts get-caller-identity
aws iam get-role --role-name LabRole --query Role.Arn
```

Esperado: a conta do laboratório e o ARN do `LabRole`. Se o segundo comando falhar, o laboratório não tem o `LabRole` e o restante não vai funcionar.

## Parte 2: criar a infraestrutura

### 2.1 Criar o bucket pela CLI

O Terraform só lê este bucket. Motivo: a política do AWS Academy impede o Terraform de criar buckets (veja "Por que o bucket não é do Terraform" no README).

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "rag-lab-docs-$ACCOUNT"
```

### 2.2 Terraform

```bash
cd ~/rag-lab
terraform init
terraform plan        # leia o que será criado (11 recursos)
terraform apply       # digite yes
```

Guarde como evidência a tela final do `terraform apply`.

## Parte 3: usar

### 3.1 Enviar os documentos e montar o índice

```bash
BUCKET=$(terraform output -raw bucket)
aws s3 cp docs-exemplo/ "s3://$BUCKET/docs/" --recursive
aws lambda invoke --function-name "$(terraform output -raw ingest_function)" /dev/stdout
```

Esperado: `{"ok": true, "chunks": N}`. Se aparecer erro, veja "Problemas comuns".

Para testar com um PDF escaneado (opcional), envie o PDF de exemplo e rode a ingestão de novo:

```bash
aws s3 cp docs-pdf/dudh.pdf "s3://$BUCKET/docs/"
aws lambda invoke --function-name "$(terraform output -raw ingest_function)" /dev/stdout
```

A resposta passa a mostrar `transcritos` e os tokens gastos (cerca de 6.500 na primeira vez). Depois a transcrição vem do cache e não gasta mais. Para usar outro PDF, troque o arquivo. Veja "PDF e consumo de tokens do Gemini" no README, e não use documento com dado pessoal real.

### 3.2 Fazer perguntas

```bash
URL=$(terraform output -raw api_url)
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d '{"question":"Qual o prazo de resposta de uma reclamação formal?"}'
```

Esperado: uma resposta com fontes no formato `[docs/...#n]`, mais `tokens_in` e `tokens_out`.

Testes extras:

```bash
# Fora da base: deve dizer que não encontrou
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d '{"question":"Qual a capital da França?"}'

# Pergunta vazia: deve devolver HTTP 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL" -H 'Content-Type: application/json' \
  -d '{"question":""}'
```

### 3.3 Ver o registro das consultas

```bash
aws dynamodb scan --table-name "$(terraform output -raw log_table)"
```

Uma linha por consulta, com tokens e latência. Guarde um registro como evidência.

## Parte 4: desligar tudo

Faça isto ao terminar, mesmo que o laboratório seja encerrado depois.

```bash
cd ~/rag-lab
terraform destroy                                  # leia a lista e digite yes
aws s3 rb "s3://rag-lab-docs-$ACCOUNT" --force     # o bucket é da CLI, o destroy não o apaga
```

Se a variável `ACCOUNT` não existir mais (sessão nova), recrie com:
`ACCOUNT=$(aws sts get-caller-identity --query Account --output text)`.

Depois confira no console que não sobrou recurso (S3, Lambda, DynamoDB, API Gateway, CloudWatch) e encerre o laboratório.

## Se a sessão do Cloud Shell expirar

A pasta `~/rag-lab` (com o `terraform.tfstate`) continua lá, mas o `/tmp` e as variáveis de ambiente foram perdidos. Para retomar:

```bash
cd ~/rag-lab
export PATH="$HOME/bin:$PATH"
export TF_DATA_DIR=/tmp/tfdata
export TF_PLUGIN_CACHE_DIR=/tmp/tfcache
mkdir -p "$TF_PLUGIN_CACHE_DIR"
export AWS_DEFAULT_REGION=us-east-1
export TF_VAR_gemini_api_key='<chave_api>'     # troque pela sua chave
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
terraform init     # baixa o provedor de novo
terraform plan     # deve mostrar "No changes" se nada mudou
```

Se o laboratório foi reiniciado (novo **Start Lab**), a conta e os recursos antigos podem ter sido apagados. Nesse caso, o `plan` vai querer criar tudo de novo: recomece da Parte 2.

## Problemas comuns

| Sintoma | Causa | O que fazer |
|---|---|---|
| `terraform: command not found` | Sessão nova, `PATH` perdido | `export PATH="$HOME/bin:$PATH"` |
| `no space left on device` no `terraform init` | `TF_DATA_DIR` e `TF_PLUGIN_CACHE_DIR` não definidos, provedor foi parar no `$HOME` | Rode o passo 1.3, apague `~/rag-lab/.terraform` e rode `terraform init` de novo |
| `terraform plan` fica parado sem mostrar nada | Sem credenciais AWS na sessão | Confira com `aws sts get-caller-identity`; reabra o Cloud Shell |
| `AccessDenied ... explicit deny in a service control policy` ao criar o bucket | Tentou criar o bucket pelo Terraform | Crie pela CLI (passo 2.1) |
| `404 ... models/... is not found` nos logs da função | Modelo do Gemini aposentado | Troque `gemini_model` em `variables.tf` e rode `terraform apply` |
| `Error acquiring the state lock` | Outro `terraform` rodando na pasta | Espere ou encerre o processo; só então use `terraform force-unlock <ID>` |
| `Inconsistent dependency lock file` | Versão do provedor mudou sem atualizar o lock | Não apague `.terraform.lock.hcl`; rode `git checkout .terraform.lock.hcl` e `terraform init` |
| Credenciais expiraram no meio | Sessão do laboratório acabou | **Start Lab** de novo e reabra o Cloud Shell |

## Segurança

- A chave do Gemini fica no `terraform.tfstate` da pasta `~/rag-lab`. Não envie essa pasta a nenhum repositório nem para a entrega.
- Não use dado pessoal real nas perguntas: o texto vai para o Google.
