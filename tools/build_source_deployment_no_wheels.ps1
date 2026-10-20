param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "deliverables\source_no_wheels"),
    [string]$PackageName = "IndustrialProtocolDemo_V19_6_14_Clean_WIN7_Source_NoWheels_20260814"
)

$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent $PSScriptRoot)
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$stage = Join-Path $output $PackageName
$zipPath = Join-Path $output ($PackageName + ".zip")
$hashPath = $zipPath + ".sha256"

New-Item -ItemType Directory -Force -Path $output | Out-Null
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
if (Test-Path -LiteralPath $hashPath) { Remove-Item -LiteralPath $hashPath -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stage "runtime") | Out-Null
Set-Content -LiteralPath (Join-Path $stage "runtime\.keep") -Value "Runtime workspace; no virtual environment is included." -Encoding ASCII

$directories = @("app", "config", "data", "data_master", "deploy", "docs", "models", "services", "tests", "tools")
foreach ($relative in $directories) {
    $source = Join-Path $root $relative
    if (Test-Path -LiteralPath $source) {
        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        & robocopy $source $destination /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS `
            /XD node_modules wheelhouse_win7 runtime_state __pycache__ .pytest_cache venv .venv model_conversion_v19_5 original_runtime_demo original_demo frozen_effect_v11_v2_inspection frozen_v11_v2_installed_smoke `
            /XF *.whl *.pyc *.pyo *.log *.rar | Out-Null
        if ($LASTEXITCODE -gt 7) {
            throw "Failed to copy source directory $relative (robocopy exit code $LASTEXITCODE)."
        }
    }
}

# The clean operator package keeps only current operational documentation and
# API contracts. Historical upgrade journals remain available in Git history.
$allowedDocs = @(
    "DATA_GOVERNANCE_V19_6.md", "DATABASE_ADMIN_GUIDE.md", "DATAMASTER_GUIDE_V19_6.md",
    "EFFECTIVENESS_ARTIFACT_CONSUMPTION_V11.md", "INTEGRATION_ARCHITECTURE.md",
    "MODEL_FIELD_MAPPING.md", "MODEL_SERVICE_DEPLOYMENT_WIN7.md",
    "MODEL_SERVICE_OUTAGE_AND_DATA_CENTER.md", "PORT_AND_STARTUP.md",
    "PRICE_NATIVE_MODEL_DYNAMIC_DEPLOYMENT.md", "PRICE_TRAINING_AND_OFFLINE_ENVIRONMENT.md",
    "CLEAN_WIN7_DEPLOYMENT_V19_6_13.md", "V19_6_14_COMPLETE_PARAMETER_PAYLOAD_AND_ADMIN_SAVE.md",
    "V19_6_11_FROZEN_EFFECT_CHANGELOG.md", "V19_6_12_RELAXED_HTTP_READINESS.md",
    "操作人员手册_测试数据运行与成品更换.md", "效能模型成品代号修改与重新打包.md",
    "甲方部署与成品更换流程_无Wheels.md"
)
$stagedDocs = Join-Path $stage "docs"
if (Test-Path -LiteralPath $stagedDocs) {
    Get-ChildItem -LiteralPath $stagedDocs -File | Where-Object {
        $isChineseOperatorMarkdown = $_.Extension -eq ".md" -and $_.Name -match '[^\x00-\x7F]'
        $allowedDocs -notcontains $_.Name -and -not $isChineseOperatorMarkdown
    } | Remove-Item -Force
}

# Keep only focused operator smoke tests.  Development benchmarks and migration
# archaeology stay in Git history, not in the installable source package.
$allowedTestFiles = @(
    "relaxed_http_contract_test.py", "service_outage_historical_fallback_test.py",
    "product_release_download_http_test.py", "complete_parameter_payload_and_admin_save_test.py"
)
$stagedTests = Join-Path $stage "tests"
if (Test-Path -LiteralPath $stagedTests) {
    Get-ChildItem -LiteralPath $stagedTests -File | Where-Object { $allowedTestFiles -notcontains $_.Name } | Remove-Item -Force
}
$allowedToolFiles = @(
    "check_model_services.py", "product_delivery.py", "verify_model_environment.py",
    "wheelhouse_manifest.py", "build_source_deployment_no_wheels.ps1"
)
$stagedTools = Join-Path $stage "tools"
if (Test-Path -LiteralPath $stagedTools) {
    Get-ChildItem -LiteralPath $stagedTools -File | Where-Object { $allowedToolFiles -notcontains $_.Name } | Remove-Item -Force
}
$experimentalGenerator = Join-Path $stage "app\gflownet_generator.py"
if (Test-Path -LiteralPath $experimentalGenerator) { Remove-Item -LiteralPath $experimentalGenerator -Force }
$publicDeployment = Join-Path $stage "deploy"
if (Test-Path -LiteralPath $publicDeployment) { Remove-Item -LiteralPath $publicDeployment -Recurse -Force }

$rootFiles = @(
    "README.md", "VERSION.txt", "requirements.txt", "run_app.py", "规范版价格预测_V19_6原生服务导出补丁.ipynb",
    "CHECK_ENVIRONMENT.bat", "CHECK_MODEL_SERVICES.bat", "INSTALL_SOURCE_DEPENDENCIES_WIN7.bat",
    "INSTALL_FROZEN_EFFECTIVENESS_MODEL_WIN7.bat", "PACKAGE_EFFECTIVENESS_SERVICE_MODEL_WIN7.bat",
    "START_PRICE_SERVICE_WIN7.bat", "START_EFFECTIVENESS_SERVICE_WIN7.bat", "START_ALL_SERVICES_WIN7.bat",
    "START_RECOMMENDATION_WITH_SERVICES_WIN7.bat", "START_ALL_NO_BROWSER.bat",
    "RUN_PRICE_TRAINING_NOTEBOOK_PY38.bat", "CREATE_PRICE_TRAINING_ENV_PY38.bat",
    "VERIFY_MODEL_ENVIRONMENTS.bat", "BUILD_SOURCE_DEPLOYMENT_NO_WHEELS.bat"
)
Get-ChildItem -LiteralPath $root -File | Where-Object { $rootFiles -contains $_.Name } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage $_.Name) -Force
}
# Avoid locale-dependent Chinese literals: this clean project has one current
# V19.6 Notebook and its original Unicode filename is preserved verbatim.
Get-ChildItem -LiteralPath $root -File -Filter "*V19_6*.ipynb" | Select-Object -First 1 | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage $_.Name) -Force
}

# Include one compact, encoded acceptance fixture plus the matching demonstrator
# model artifacts.  Raw generation workspaces and old deliveries are omitted.
$fixtureDestination = Join-Path $stage "examples\final_acceptance"
New-Item -ItemType Directory -Force -Path $fixtureDestination | Out-Null
$finalFixture = Join-Path $root "outputs\final_acceptance_fixture_20260814"
foreach ($name in @("encoded_aircraft_door_lock_prediction.xlsx", "expert_state_v10.json", "expert_state_v11.json")) {
    $sourceFile = Join-Path $finalFixture $name
    if (Test-Path -LiteralPath $sourceFile) {
        Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $fixtureDestination $name) -Force
    }
}
$modelFixture = Join-Path $root "outputs\019fb26c_basic_aircraft_door_lock_models_20260812"
foreach ($relative in @("price", "effectiveness_runtime")) {
    $sourceDirectory = Join-Path $modelFixture $relative
    if (Test-Path -LiteralPath $sourceDirectory) {
        $destinationDirectory = Join-Path $fixtureDestination $relative
        & robocopy $sourceDirectory $destinationDirectory /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS `
            /XD runtime_state __pycache__ /XF *.pyc *.pyo *.log | Out-Null
        if ($LASTEXITCODE -gt 7) { throw "Failed to copy final acceptance model fixture: $relative" }
    }
}

$excludedDirectoryNames = @("wheelhouse_win7", "runtime_state", "__pycache__", ".pytest_cache", "node_modules", "venv", ".venv", "model_conversion_v19_5", "original_runtime_demo", "original_demo", "frozen_effect_v11_v2_inspection", "frozen_v11_v2_installed_smoke")
Get-ChildItem -LiteralPath $stage -Directory -Recurse | Where-Object {
    $excludedDirectoryNames -contains $_.Name
} | Sort-Object FullName -Descending | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

$excludedExtensions = @(".whl", ".pyc", ".pyo", ".log", ".rar")
Get-ChildItem -LiteralPath $stage -File -Recurse | Where-Object {
    $excludedExtensions -contains $_.Extension.ToLowerInvariant()
} | Remove-Item -Force

$manifest = [ordered]@{
    package_name = $PackageName
    generated_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
    package_type = "installable-source-with-model-artifacts"
    source_scope = "recommendation-and-http-model-services"
    automl_pipeline_included = $false
    wheels_included = $false
    virtual_environment_included = $false
    effectiveness_runtime = "services/effectiveness_service/model/current/effectiveness_runtime_manifest.json"
    price_runtime = "services/price_service/model/price_native_bundle.pkl"
    production_candidate_generator = "V19.6.14 coupling-aware fast/deep beam search"
    effectiveness_model_install = "INSTALL_FROZEN_EFFECTIVENESS_MODEL_WIN7.bat (recommended frozen ZIP) or PACKAGE_EFFECTIVENESS_SERVICE_MODEL_WIN7.bat (legacy compatible)"
    effectiveness_workbench = "http://127.0.0.1:17891/effectiveness"
    price_workbench = "http://127.0.0.1:17891/price"
    install_script = "INSTALL_SOURCE_DEPENDENCIES_WIN7.bat"
    start_script = "START_ALL_SERVICES_WIN7.bat"
    excluded = @("wheelhouse_win7", "runtime/venvs", "backups", "logs", "*.whl", "*.pyc", "*.log", "handoff archives", "historical upgrade docs", "experimental generators", "legacy demo runtimes")
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stage "SOURCE_PACKAGE_MANIFEST.json") -Encoding UTF8

$guideLines = @(
    "# Source deployment package without wheels",
    "",
    "This package includes the recommendation system, model services, current model artifacts, test data, documentation and tests. It does not include wheels or a virtual environment.",
    "This is the recommendation/operator deployment package. The separate AutoML V4 training pipeline lives in automl_pipeline/ in the Git repository and is not part of this package.",
    "",
    "1. Install 64-bit Python 3.8.",
    "2. With Internet or an internal pip mirror, run INSTALL_SOURCE_DEPENDENCIES_WIN7.bat.",
    "3. For an offline target, provide dependency wheels separately or copy an accepted runtime\venvs\model_runtime38. Wheels are intentionally absent here.",
    "4. Run VERIFY_MODEL_ENVIRONMENTS.bat.",
    "5. Run START_ALL_SERVICES_WIN7.bat. Readiness requires real numeric price/effectiveness JSON responses; Schema and product-code differences are operator warnings, not preflight blockers.",
    "6. Open http://127.0.0.1:17891/; use /admin for data maintenance, /price for price-only prediction and /effectiveness for effectiveness-only evaluation.",
    "7. In Product Data Workspace, ordinary historical CSV/XLSX data can be analyzed, edited and switched independently of the currently running HTTP model product.",
    "8. A business/model mismatch pauses model calculations only; it does not block data maintenance, switching, or historical-product recommendation.",
    "9. Open the single *V19_6*.ipynb file in the package. Set only PRODUCT_CODE in the final cell; any fitted model subset is accepted and installed directly as price_native_bundle.pkl.",
    "10. The data center model page reads HTTP health/schema and produces example JSON. It does not load local model files in service mode.",
    "11. Use examples/final_acceptance for the encoded English-field workbook, V10/V11 expert-state JSON, and matching virtual model artifacts.",
    "",
    "Online effectiveness learning is external. Prefer INSTALL_FROZEN_EFFECTIVENESS_MODEL_WIN7.bat for effectiveness_model_*.zip exported by the final V11 expert software.",
    "The legacy source + Workbook + State path remains available through PACKAGE_EFFECTIVENESS_SERVICE_MODEL_WIN7.bat."
)
$guideLines | Set-Content -LiteralPath (Join-Path $stage "DEPLOYMENT_README_NO_WHEELS.md") -Encoding UTF8
@(
    "# Recommendation source deployment package",
    "",
    "This operator package contains the recommendation application and HTTP model services.",
    "Read DEPLOYMENT_README_NO_WHEELS.md for setup and startup instructions.",
    "The AutoML V2/V3/V4 source area is maintained separately under automl_pipeline/ in the Git repository."
) | Set-Content -LiteralPath (Join-Path $stage "README.md") -Encoding UTF8

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
("{0}  {1}" -f $hash, [System.IO.Path]::GetFileName($zipPath)) | Set-Content -LiteralPath $hashPath -Encoding ASCII

$wheelCount = @(Get-ChildItem -LiteralPath $stage -File -Recurse -Filter "*.whl").Count
$pycCount = @(
    Get-ChildItem -LiteralPath $stage -File -Recurse | Where-Object {
        $_.Extension.ToLowerInvariant() -in @(".pyc", ".pyo")
    }
).Count
if ($wheelCount -ne 0 -or $pycCount -ne 0) {
    throw "Excluded binary dependency/cache files remain in the staged package."
}
if (-not (Test-Path -LiteralPath (Join-Path $stage "services\effectiveness_service\model\current\effectiveness_runtime_manifest.json"))) {
    throw "Current effectiveness artifact is missing from the staged package."
}
if (-not (Test-Path -LiteralPath (Join-Path $stage "services\price_service\model\price_native_bundle.pkl"))) {
    throw "Current price artifact is missing from the staged package."
}

[pscustomobject]@{
    Package = $zipPath
    Sha256 = $hash
    SizeMB = [Math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 2)
    Wheels = $wheelCount
} | Format-List
