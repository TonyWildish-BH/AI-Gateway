
resource_group_name     = "lab-backend-pool-load-balancing-tf"
resource_group_location = "swedencentral"
apim_sku                = "BasicV2_1"
# model_deployment_name / model_name / model_version are intentionally unset:
# they resolve from shared/models.json (role 'chat-small-4o') in main.tf.
model_capacity    = "1"
model_api_version = "2024-10-21"
aiservices_config = {
  aiservices-uks = {
    name     = "foundry1",
    location = "swedencentral",
    priority = 1
    weight   = ""
  },
  aiservices-swc = {
    name     = "foundry2",
    location = "swedencentral",
    priority = 2,
    weight   = 50
  },
  aiservices-frc = {
    name     = "foundry3",
    location = "swedencentral",
    priority = 2,
    weight   = 50
  }
}

