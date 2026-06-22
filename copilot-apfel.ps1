#!/usr/bin/env pwsh
#Requires -Version 7.4

<#
.SYNOPSIS
    Runs GitHub Copilot CLI against the local apfel server (Apple FoundationModels)
    via Copilot CLI's BYOK provider, routed through apfel_proxy.py.

.DESCRIPTION
    A raw Copilot CLI request is ~107k tokens (226 tool schemas ~103k +
    system prompt ~6.2k), which overflows apfel's hard 4096-token context window:
        "400 Input exceeds the 4096-token context window."

    apfel_proxy.py strips tool schemas, truncates the system prompt, and rolls
    history into local files (~/.apfel-copilot/) so each request fits 4096.
    Because tools are stripped, this is a working CHAT against the on-device
    model, NOT the full file-editing / shell agent. That is the price of 4096.

.PARAMETER ApfelUrl
    Base URL of the apfel OpenAI-compatible server.

.PARAMETER ProxyPort
    TCP port the context-fitting proxy listens on.

.PARAMETER Model
    apfel model id reported by the server.

.PARAMETER MaxPromptTokens
    BYOK prompt-token budget advertised to Copilot CLI.

.PARAMETER MaxOutputTokens
    BYOK output-token budget advertised to Copilot CLI.

.PARAMETER Prompt
    Convenience prompt forwarded to Copilot CLI as `copilot -p <Prompt>`
    (non-interactive). Omit for an interactive session.

.PARAMETER CopilotArgs
    Additional arguments passed verbatim to the `copilot` executable, appended
    after any -Prompt mapping (e.g. -CopilotArgs '--allow-all-tools').

.EXAMPLE
    ./copilot-apfel.ps1 -Prompt "Explain TCP/IP in one sentence."

.EXAMPLE
    ./copilot-apfel.ps1 -ProxyPort 9001

.EXAMPLE
    ./copilot-apfel.ps1            # interactive session
#>
[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string] $ApfelUrl = 'http://localhost:11434/v1',

    [Parameter()]
    [ValidateRange(1, 65535)]
    [int] $ProxyPort = 8898,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string] $Model = 'apple-foundationmodel',

    [Parameter()]
    [ValidateRange(1, 4096)]
    [int] $MaxPromptTokens = 3500,

    [Parameter()]
    [ValidateRange(1, 4096)]
    [int] $MaxOutputTokens = 512,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string] $Prompt,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CopilotArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-ApfelEndpoint {
    <#
    .SYNOPSIS
        Returns $true when an OpenAI-compatible /models endpoint is reachable.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $BaseUrl,

        [Parameter()]
        [ValidateRange(1, 60)]
        [int] $TimeoutSeconds = 3
    )

    try {
        Invoke-WebRequest -Uri "$BaseUrl/models" -TimeoutSec $TimeoutSeconds -UseBasicParsing |
            Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-ApfelEndpoint {
    <#
    .SYNOPSIS
        Polls an endpoint until it is reachable or the attempt budget is spent.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $BaseUrl,

        [Parameter()]
        [ValidateRange(1, 120)]
        [int] $MaxAttempts = 15,

        [Parameter()]
        [ValidateRange(1, 30)]
        [int] $DelaySeconds = 1
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        if (Test-ApfelEndpoint -BaseUrl $BaseUrl) {
            return $true
        }
        Start-Sleep -Seconds $DelaySeconds
    }
    return (Test-ApfelEndpoint -BaseUrl $BaseUrl)
}

function Start-ApfelServer {
    <#
    .SYNOPSIS
        Ensures the apfel --serve OpenAI-compatible server is running.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([void])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $BaseUrl,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $LogDirectory
    )

    if (Test-ApfelEndpoint -BaseUrl $BaseUrl) {
        Write-Verbose "apfel server already running at $BaseUrl."
        return
    }

    if (-not (Get-Command -Name 'apfel' -ErrorAction SilentlyContinue)) {
        throw "'apfel' executable not found on PATH. Install it (brew install apfel)."
    }

    if ($PSCmdlet.ShouldProcess('apfel --serve', 'Start')) {
        Write-Information -MessageData "apfel server not up; starting 'apfel --serve'..." -InformationAction Continue
        $startParams = @{
            FilePath               = 'apfel'
            ArgumentList           = '--serve'
            RedirectStandardOutput = Join-Path $LogDirectory 'apfel-serve.log'
            RedirectStandardError  = Join-Path $LogDirectory 'apfel-serve.err.log'
        }
        Start-Process @startParams | Out-Null

        if (-not (Wait-ApfelEndpoint -BaseUrl $BaseUrl)) {
            throw "apfel server did not become ready at $BaseUrl."
        }
    }
}

function Start-ApfelProxy {
    <#
    .SYNOPSIS
        Ensures the context-fitting apfel_proxy.py is running on the given port.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([void])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $BaseUrl,

        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int] $Port,

        [Parameter(Mandatory)]
        [ValidateScript({ Test-Path -Path $_ -PathType Leaf })]
        [string] $ScriptPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $LogDirectory
    )

    if (Test-ApfelEndpoint -BaseUrl $BaseUrl) {
        Write-Verbose "apfel proxy already running at $BaseUrl."
        return
    }

    $python = Get-Command -Name 'python3', 'python' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $python) {
        throw "Neither 'python3' nor 'python' found on PATH; required to run apfel_proxy.py."
    }

    if ($PSCmdlet.ShouldProcess("apfel_proxy.py on :$Port", 'Start')) {
        Write-Information -MessageData "starting apfel_proxy.py on :$Port..." -InformationAction Continue
        $proxyEnvironment = @{
            APFEL_PROXY_PORT = "$Port"
        }
        $startParams = @{
            FilePath               = $python.Source
            ArgumentList           = $ScriptPath
            Environment            = $proxyEnvironment
            RedirectStandardOutput = Join-Path $LogDirectory 'apfel-proxy.log'
            RedirectStandardError  = Join-Path $LogDirectory 'apfel-proxy.err.log'
        }
        Start-Process @startParams | Out-Null

        if (-not (Wait-ApfelEndpoint -BaseUrl $BaseUrl -MaxAttempts 10)) {
            throw "apfel proxy did not become ready at $BaseUrl."
        }
    }
}

function Set-CopilotProviderEnvironment {
    <#
    .SYNOPSIS
        Configures the COPILOT_PROVIDER_* BYOK environment for Copilot CLI.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([void])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $BaseUrl,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $Model,

        [Parameter(Mandatory)]
        [ValidateRange(1, 4096)]
        [int] $MaxPromptTokens,

        [Parameter(Mandatory)]
        [ValidateRange(1, 4096)]
        [int] $MaxOutputTokens
    )

    if (-not $PSCmdlet.ShouldProcess('Copilot CLI', 'Configure BYOK provider environment')) {
        return
    }

    $env:COPILOT_PROVIDER_BASE_URL          = $BaseUrl
    $env:COPILOT_PROVIDER_TYPE              = 'openai'        # apfel = OpenAI-compatible
    $env:COPILOT_PROVIDER_API_KEY           = 'apfel-local'   # apfel needs none; dummy keeps CLI happy
    $env:COPILOT_PROVIDER_WIRE_MODEL        = $Model          # name sent on the wire
    $env:COPILOT_PROVIDER_MODEL_ID          = $Model          # well-known id for limits/agent cfg
    $env:COPILOT_MODEL                      = $Model
    $env:COPILOT_PROVIDER_MAX_PROMPT_TOKENS = "$MaxPromptTokens"
    $env:COPILOT_PROVIDER_MAX_OUTPUT_TOKENS = "$MaxOutputTokens"
    $env:COPILOT_OFFLINE                    = '1'             # skip GitHub auth/telemetry/web/auto-update
}

function Invoke-CopilotApfel {
    <#
    .SYNOPSIS
        Orchestrates apfel + proxy startup, BYOK config, and the copilot call.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $ApfelUrl,

        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int] $ProxyPort,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $Model,

        [Parameter(Mandatory)]
        [ValidateRange(1, 4096)]
        [int] $MaxPromptTokens,

        [Parameter(Mandatory)]
        [ValidateRange(1, 4096)]
        [int] $MaxOutputTokens,

        [Parameter()]
        [AllowEmptyString()]
        [string] $Prompt,

        [Parameter()]
        [AllowEmptyCollection()]
        [string[]] $CopilotArgs = @()
    )

    if (-not (Get-Command -Name 'copilot' -ErrorAction SilentlyContinue)) {
        throw "'copilot' executable not found on PATH. Install GitHub Copilot CLI."
    }

    $scriptRoot   = Split-Path -Parent $PSCommandPath
    $proxyScript  = Join-Path $scriptRoot 'apfel_proxy.py'
    $proxyUrl     = "http://localhost:$ProxyPort/v1"
    $logDirectory = [System.IO.Path]::GetTempPath()

    Start-ApfelServer -BaseUrl $ApfelUrl -LogDirectory $logDirectory
    Start-ApfelProxy  -BaseUrl $proxyUrl -Port $ProxyPort -ScriptPath $proxyScript -LogDirectory $logDirectory

    $providerParams = @{
        BaseUrl         = $proxyUrl
        Model           = $Model
        MaxPromptTokens = $MaxPromptTokens
        MaxOutputTokens = $MaxOutputTokens
    }
    Set-CopilotProviderEnvironment @providerParams

    $invocationArgs = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($Prompt)) {
        $invocationArgs.Add('-p')
        $invocationArgs.Add($Prompt)
    }
    if ($CopilotArgs) {
        $invocationArgs.AddRange($CopilotArgs)
    }

    & copilot @invocationArgs
    return $LASTEXITCODE
}

# Entry point: only run when invoked as a script (not when dot-sourced for tests).
if ($MyInvocation.InvocationName -ne '.') {
    # Honour environment overrides only when the caller did not pass the parameter.
    if (-not $PSBoundParameters.ContainsKey('ApfelUrl') -and -not [string]::IsNullOrEmpty($env:APFEL_URL)) {
        $ApfelUrl = $env:APFEL_URL
    }
    if (-not $PSBoundParameters.ContainsKey('ProxyPort') -and -not [string]::IsNullOrEmpty($env:APFEL_PROXY_PORT)) {
        $ProxyPort = [int] $env:APFEL_PROXY_PORT
    }

    $invokeParams = @{
        ApfelUrl        = $ApfelUrl
        ProxyPort       = $ProxyPort
        Model           = $Model
        MaxPromptTokens = $MaxPromptTokens
        MaxOutputTokens = $MaxOutputTokens
        Prompt          = $Prompt
        CopilotArgs     = $CopilotArgs
    }
    $exitCode = Invoke-CopilotApfel @invokeParams
    exit $exitCode
}
