targetScope = 'resourceGroup'

param botName string = 'teams-acl-demo-sp-remote-bot'
param projectEndpoint string
param agentName string = 'teams-acl-demo-sp-remote-agent'
param agentClientId string
param tenantId string

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: 'Teams SharePoint ACL Demo (Remote)'
    endpoint: '${projectEndpoint}/agents/${agentName}/endpoint/protocols/activityProtocol?api-version=v1'
    msaAppId: agentClientId
    msaAppTenantId: tenantId
    msaAppType: 'SingleTenant'
    publicNetworkAccess: 'Disabled'
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2021-03-01' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
  }
}

output botId string = bot.id
