targetScope = 'resourceGroup'

param searchServiceName string
param projectResourceId string
param principalId string

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchServiceName
}

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '1407120a-92aa-4202-b7e9-c0e197c71c8f')

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, projectResourceId, roleId)
  scope: search
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleId
  }
}
