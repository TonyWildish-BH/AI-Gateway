// ------------------
//    PARAMETERS
// ------------------

param aiServicesConfig array = []
param modelsConfig array = []
param apimSku string
param apimSubscriptionsConfig array = []
param inferenceAPIType string = 'AzureOpenAI'
param inferenceAPIPath string = 'inference' // Path to the inference API in the APIM service
param foundryProjectName string = 'default'

// ------------------
//    VARIABLES
// ------------------

// policy.xml routes on the model name the client asks for, so those names must match
// what is actually deployed. They are {placeholders} in the XML and are filled in here
// from the central catalogue (shared/models.json) - never hardcoded in the policy.
var modelCatalogPath = '../../shared/models.json'
var routedPolicyXml = replace(replace(replace(replace(replace(replace(
  loadTextContent('policy.xml'),
  '{model-tier1}',       loadJsonContent(modelCatalogPath, '$.foundry.chat-standard.name')),
  '{model-tier2-small}', loadJsonContent(modelCatalogPath, '$.foundry.chat-small.name')),
  '{model-tier2-nano}',  loadJsonContent(modelCatalogPath, '$.foundry.chat-nano.name')),
  '{router-tier-model}',  loadJsonContent(modelCatalogPath, '$.foundry.router.name')),
  '{model-tier3}',       loadJsonContent(modelCatalogPath, '$.foundry.deepseek-chat.name')),
  '{model-blocked}',      'gpt-4o')  // model-catalog-allow: blocklist demo - a retired family the gateway refuses; deliberately NOT a catalogue role, because the point is that it is no longer deployable

// ------------------
//    RESOURCES
// ------------------

// 1. Log Analytics Workspace
module lawModule '../../modules/operational-insights/v1/workspaces.bicep' = {
  name: 'lawModule'
}

// 2. Application Insights
module appInsightsModule '../../modules/monitor/v1/appinsights.bicep' = {
  name: 'appInsightsModule'
  params: {
    lawId: lawModule.outputs.id
    customMetricsOptedInType: 'WithDimensions'
  }
}

// 3. API Management
module apimModule '../../modules/apim/v2/apim.bicep' = {
  name: 'apimModule'
  params: {
    apimSku: apimSku
    apimSubscriptionsConfig: apimSubscriptionsConfig
    lawId: lawModule.outputs.id
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
  }
}

// 4. AI Foundry
module foundryModule '../../modules/cognitive-services/v3/foundry.bicep' = {
    name: 'foundryModule'
    params: {
      aiServicesConfig: aiServicesConfig
      modelsConfig: modelsConfig
      apimPrincipalId: apimModule.outputs.principalId
      foundryProjectName: foundryProjectName
    }
  }

// 5. APIM Inference API
module inferenceAPIModule '../../modules/apim/v2/inference-api.bicep' = {
  name: 'inferenceAPIModule'
  params: {
    policyXml: routedPolicyXml
    apimLoggerId: apimModule.outputs.loggerId
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
    aiServicesConfig: foundryModule.outputs.extendedAIServicesConfig
    inferenceAPIType: inferenceAPIType
    inferenceAPIPath: inferenceAPIPath
  }
}


// ------------------
//    OUTPUTS
// ------------------

output logAnalyticsWorkspaceId string = lawModule.outputs.customerId
output apimServiceId string = apimModule.outputs.id
output apimResourceGatewayURL string = apimModule.outputs.gatewayUrl

output apimSubscriptions array = apimModule.outputs.apimSubscriptions
