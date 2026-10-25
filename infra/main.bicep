targetScope = 'resourceGroup'

param accountName string
param projectName string = 'teams-acl-demo'
param location string = 'westus'
param searchResourceGroup string
param searchServiceName string
param userAObjectId string
param userBObjectId string

resource account 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Teams SharePoint ACL demo'
    description: 'Isolated project for the two-user synthetic ACL pilot.'
  }
}

var consumerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6')

resource consumers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in [
  userAObjectId
  userBObjectId
]: {
  name: guid(project.id, principalId, consumerRoleId)
  scope: project
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: consumerRoleId
  }
}]

module searchReader 'search-reader.bicep' = {
  name: 'teams-acl-demo-search-reader'
  scope: resourceGroup(searchResourceGroup)
  params: {
    searchServiceName: searchServiceName
    projectResourceId: project.id
    principalId: project.identity.principalId
  }
}

output projectId string = project.id
