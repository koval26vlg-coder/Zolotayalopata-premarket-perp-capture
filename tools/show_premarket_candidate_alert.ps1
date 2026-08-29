[CmdletBinding()]
param(
    [switch]$Preflight,
    [switch]$Show,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resultSchema = 'premarket_candidate_alert_toast_result_v1'
$preflightSchema = 'premarket_candidate_alert_toast_preflight_v1'
$payloadSchema = 'premarket_candidate_alert_toast_v1'
$alertGroup = 'ZLP-PREMARKET'
$showInvoked = $false

function Write-Result {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Value)
    if ($Json) {
        $Value | ConvertTo-Json -Compress -Depth 16
    } else {
        $Value
    }
}

function Require-CanonicalText {
    param(
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string]$Field,
        [int]$MaxLength = 2048
    )
    if ($Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace($Value) -or
        $Value -ne $Value.Trim() -or
        $Value.Length -gt $MaxLength -or
        $Value.IndexOfAny([char[]](0..31)) -ge 0) {
        throw "$Field is missing or non-canonical"
    }
    return [string]$Value
}

function Get-NotifierContext {
    $apps = @(
        Get-StartApps |
            Where-Object { $_.Name -eq 'PowerShell 7 (x64)' }
    )
    if ($apps.Count -ne 1) {
        throw "Expected exactly one PowerShell 7 (x64) Start app, found $($apps.Count)"
    }
    $appId = Require-CanonicalText -Value $apps[0].AppID -Field 'app_id' -MaxLength 256
    $managerType = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $xmlType = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
    $toastType = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $notifier = $managerType::CreateToastNotifier($appId)
    if ($null -eq $notifier) {
        throw 'Toast notifier could not be created'
    }
    $history = $managerType::History
    if ($null -eq $history) {
        throw 'Toast notification history is unavailable'
    }
    # This read proves that the discovered AUMID is accepted by Notification Center.
    $null = $history.GetHistory($appId)
    return [ordered]@{
        app_id = $appId
        manager_type = $managerType
        xml_type = $xmlType
        toast_type = $toastType
        notifier = $notifier
        history = $history
    }
}

function Test-OfficialArticleUrl {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$ListingVenue
    )
    $uri = [Uri]$Url
    if (-not $uri.IsAbsoluteUri -or
        $uri.Scheme -ne 'https' -or
        -not [string]::IsNullOrEmpty($uri.UserInfo) -or
        -not $uri.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($uri.Query) -or
        -not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw 'article_url is not canonical HTTPS'
    }
    $hostName = $uri.DnsSafeHost.ToLowerInvariant()
    $path = $uri.AbsolutePath
    $allowed = $false
    if ($ListingVenue -eq 'bybit') {
        $allowed = (($hostName -eq 'announcements.bybit.com' -and $path.Contains('/article/')) -or
                    ($hostName -eq 'www.bybit.com' -and $path.StartsWith('/en/help-center/article/')))
    } elseif ($ListingVenue -eq 'bitget') {
        $allowed = ($hostName -eq 'www.bitget.com' -and $path.StartsWith('/support/articles/'))
    } elseif ($ListingVenue -eq 'kucoin') {
        $allowed = ($hostName -eq 'www.kucoin.com' -and $path.StartsWith('/announcement/'))
    }
    if (-not $allowed) {
        throw "article_url is not official for $ListingVenue"
    }
}

if ($Preflight.IsPresent -eq $Show.IsPresent) {
    throw 'Choose exactly one mode: -Preflight or -Show.'
}

try {
    $context = Get-NotifierContext
    if ($Preflight) {
        Write-Result -Value ([ordered]@{
            schema = $preflightSchema
            status = 'READY'
            app_id = $context.app_id
            show_invoked = $false
        })
        exit 0
    }

    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw 'toast payload is missing'
    }
    $payload = $raw | ConvertFrom-Json
    if ($null -eq $payload -or $payload -isnot [pscustomobject]) {
        throw 'toast payload is not an object'
    }
    $expectedFields = @(
        'schema', 'notification_id', 'alert_kind', 'candidate_id',
        'candidate_record_hash', 'episode_id', 'ticker', 'perpetual_venue',
        'premarket_contract_id', 'listing_venue', 'article_title', 'article_url',
        'review_state', 'capture_authorized', 'tag', 'group'
    )
    $actualFields = @($payload.PSObject.Properties.Name | Sort-Object)
    $expectedSorted = @($expectedFields | Sort-Object)
    if (($actualFields -join "`n") -ne ($expectedSorted -join "`n")) {
        throw 'toast payload fields do not match the exact schema'
    }
    if ($payload.schema -ne $payloadSchema -or
        $payload.alert_kind -ne 'UNVERIFIED_ANNOUNCEMENT_CANDIDATE' -or
        $payload.review_state -ne 'HUMAN_ATTESTATION_REQUIRED' -or
        $payload.capture_authorized -ne $false -or
        $payload.group -ne $alertGroup) {
        throw 'toast payload attempts to cross the non-authority boundary'
    }
    foreach ($field in @('notification_id', 'candidate_id', 'candidate_record_hash')) {
        $value = Require-CanonicalText -Value $payload.$field -Field $field -MaxLength 64
        if ($value -notmatch '^[0-9a-f]{64}$') {
            throw "$field is not a SHA-256 value"
        }
    }
    $tag = Require-CanonicalText -Value $payload.tag -Field 'tag' -MaxLength 16
    if ($tag -notmatch '^[0-9a-f]{16}$' -or
        $tag -ne $payload.notification_id.Substring(0, 16)) {
        throw 'toast tag does not match notification_id'
    }
    foreach ($field in @(
        'episode_id', 'ticker', 'perpetual_venue', 'premarket_contract_id',
        'listing_venue', 'article_title', 'article_url'
    )) {
        $null = Require-CanonicalText -Value $payload.$field -Field $field
    }
    Test-OfficialArticleUrl -Url $payload.article_url -ListingVenue $payload.listing_venue

    $document = New-Object $context.xml_type
    $toastElement = $document.CreateElement('toast')
    $toastElement.SetAttribute('activationType', 'protocol')
    $toastElement.SetAttribute('launch', [string]$payload.article_url)
    $null = $document.AppendChild($toastElement)
    $visual = $document.CreateElement('visual')
    $null = $toastElement.AppendChild($visual)
    $binding = $document.CreateElement('binding')
    $binding.SetAttribute('template', 'ToastGeneric')
    $null = $visual.AppendChild($binding)
    foreach ($textValue in @(
        "ZolotyayLopata - listing candidate $($payload.ticker)",
        "$($payload.perpetual_venue):$($payload.premarket_contract_id) -> spot $($payload.listing_venue)",
        'Human verification of official t0 is required. Capture is not running.'
    )) {
        $textNode = $document.CreateElement('text')
        $textNode.InnerText = $textValue
        $null = $binding.AppendChild($textNode)
    }

    $notification = New-Object $context.toast_type -ArgumentList $document
    $notification.Tag = $tag
    $notification.Group = $alertGroup
    $showInvoked = $true
    $context.notifier.Show($notification)

    $historyConfirmed = $false
    for ($attempt = 0; $attempt -lt 4 -and -not $historyConfirmed; $attempt++) {
        if ($attempt -gt 0) {
            Start-Sleep -Milliseconds 250
        }
        $historyConfirmed = @(
            $context.history.GetHistory($context.app_id) |
                Where-Object { $_.Tag -eq $tag -and $_.Group -eq $alertGroup }
        ).Count -gt 0
    }
    $status = if ($historyConfirmed) {
        'WINDOWS_HISTORY_CONFIRMED'
    } else {
        'WINDOWS_SUBMITTED_UNCONFIRMED'
    }
    Write-Result -Value ([ordered]@{
        schema = $resultSchema
        status = $status
        notification_id = [string]$payload.notification_id
        show_invoked = $true
        tag = $tag
        group = $alertGroup
        app_id = $context.app_id
    })
    exit 0
} catch {
    $failure = [ordered]@{
        schema = $resultSchema
        status = 'NOTIFIER_ERROR'
        show_invoked = $showInvoked
        reason = $_.Exception.Message
    }
    Write-Result -Value $failure
    [Console]::Error.WriteLine(($failure | ConvertTo-Json -Compress -Depth 16))
    exit 1
}
