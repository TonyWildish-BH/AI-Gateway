import datetime, json, os, subprocess, requests, time, traceback

# Define ANSI escape code constants vor clarity in the print commands below
RESET_FORMATTING = "\x1b[0m"
BOLD_BLUE = "\x1b[1;34m"
BOLD_RED = "\x1b[1;31m"
BOLD_GREEN = "\x1b[1;32m"
BOLD_YELLOW = "\x1b[1;33m"

print_command = lambda command='': print(f"⚙️ {BOLD_BLUE}Running: {command} {RESET_FORMATTING}")
print_error = lambda message, output='', duration='': print(f"❌ {BOLD_YELLOW}{message}{RESET_FORMATTING} ⌚ {datetime.datetime.now().time()} {duration}{' ' if output else ''}{output}")
print_info = lambda message: print(f"👉🏽 {BOLD_BLUE}{message}{RESET_FORMATTING}")
print_message = lambda message, output='', duration='': print(f"👉🏽 {BOLD_GREEN}{message}{RESET_FORMATTING} ⌚ {datetime.datetime.now().time()} {duration}{' ' if output else ''}{output}")
print_ok = lambda message, output='', duration='': print(f"✅ {BOLD_GREEN}{message}{RESET_FORMATTING} ⌚ {datetime.datetime.now().time()} {duration}{' ' if output else ''}{output}")
print_warning = lambda message, output='', duration='': print(f"⚠️ {BOLD_YELLOW}{message}{RESET_FORMATTING} ⌚ {datetime.datetime.now().time()} {duration}{' ' if output else ''}{output}")

class Output(object):
    def __init__(self, success, text):
        self.success = success
        self.text = text

        try:
            self.json_data = json.loads(text)
        except:
            self.json_data = json.loads("{}")   # return an empty JSON object if the output is not valid JSON rather than None as that makes consuming it easier this way


def get_current_subscription():
    try:
        output = run("az account show", "Retrieved az account", "Failed to get the current az account")

        if output.success and output.json_data:
            subscription_id = output.json_data['id']
            subscription_name = output.json_data['name']
            print_info(f"Using Subscription ID: {subscription_id} ({subscription_name})")
            return subscription_id
        else:
            print_error("No current subscription found.")
            return None
    except Exception as e:
        print_error(f"Error retrieving current subscription: {e}")
        return None

MODEL_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models.json')

# Keys that describe the model itself and are therefore owned by the catalog.
# Anything else a lab passes (capacity, aiservice, policies, meter SKUs, ...) is lab-specific.
MODEL_CATALOG_KEYS = ('name', 'publisher', 'version', 'sku')

_model_catalog = None

def load_model_catalog(catalog_path = None):
    global _model_catalog

    if catalog_path is None and _model_catalog is not None:
        return _model_catalog

    path = catalog_path or MODEL_CATALOG_PATH
    try:
        with open(path, 'r', encoding='utf-8') as catalog_file:
            catalog = json.load(catalog_file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Model catalog not found at '{path}'. It should live at shared/models.json.")
    except json.JSONDecodeError as e:
        raise ValueError(f"Model catalog '{path}' is not valid JSON: {e}")

    if 'foundry' not in catalog:
        raise ValueError(f"Model catalog '{path}' is missing the 'foundry' section.")

    if catalog_path is None:
        _model_catalog = catalog

    return catalog

def get_model(role, catalog_path = None):
    """Resolve a catalog role (or a literal model name via the alias table) to its definition."""
    catalog = load_model_catalog(catalog_path)
    foundry = catalog['foundry']

    resolved_role = role
    if resolved_role not in foundry:
        resolved_role = catalog.get('aliases', {}).get(role)
        if resolved_role is None or resolved_role not in foundry:
            available = ', '.join(sorted(r for r in foundry if not r.startswith('$')))
            raise KeyError(f"Unknown model role '{role}'. Available roles: {available}")
        print_warning(f"'{role}' is a literal model name; use the role '{resolved_role}' instead so deprecations stay a one-line change")

    return resolved_role, foundry[resolved_role]

def model_name(role, catalog_path = None):
    """Return just the deployment name for a role, for use in client calls and URLs."""
    return get_model(role, catalog_path)[1]['name']

def external_model_name(role, catalog_path = None):
    """Return the model name for a role in the catalog's 'external' section.

    External models (AWS Bedrock, Google Gemini, Ollama, self-hosted SLMs) are not
    deployed through Azure Cognitive Services, so they carry no publisher/version/sku
    and cannot go through get_model()/models_config().
    """
    external = load_model_catalog(catalog_path).get('external', {})
    if role not in external or role.startswith('$'):
        available = ', '.join(sorted(r for r in external if not r.startswith('$')))
        raise KeyError(f"Unknown external model role '{role}'. Available roles: {available}")

    return external[role]['name']

def models_config(*requested, catalog_path = None):
    """Build a models_config array from catalog roles.

    Each argument is either a role name, or a (role, overrides) tuple where overrides
    supplies lab-specific values such as capacity, aiservice or policies:

        models_config = utils.models_config(('chat-small', {"capacity": 20}))
        models_config = utils.models_config(
            ('chat-standard', {"capacity": 20, "aiservice": "foundry1"}),
            ('chat-nano',     {"capacity": 20, "aiservice": "foundry2"}),
        )
    """
    resolved = []
    non_ga = []

    for entry in requested:
        if isinstance(entry, str):
            role, overrides = entry, {}
        else:
            role, overrides = entry

        resolved_role, definition = get_model(role, catalog_path)

        model = {key: definition[key] for key in MODEL_CATALOG_KEYS}
        model['capacity'] = definition.get('defaultCapacity', 20)
        model.update(overrides)
        resolved.append(model)

        lifecycle = definition.get('lifecycle')
        if lifecycle and lifecycle != 'GenerallyAvailable':
            non_ga.append((resolved_role, model['name'], lifecycle, definition.get('notes', '')))

    for resolved_role, name, lifecycle, notes in non_ga:
        print_warning(f"Model '{name}' (role '{resolved_role}') is '{lifecycle}' as of {load_model_catalog(catalog_path).get('lifecycleCheckedOn', 'the last catalog refresh')}. {notes}".strip())

    return resolved

def validate_model_definitions(subscription_id, location, models_config):
    if not subscription_id:
        raise ValueError("Missing subscription ID parameter.")
    if not location:
        raise ValueError("Missing Azure region parameter.")

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CognitiveServices/locations/{location}/models"
        "?api-version=2024-10-01"
    )
    output = run(
        f'az rest --method get --url "{url}" --output json',
        "Retrieved the regional model catalog",
        "Failed to retrieve the regional model catalog",
    )
    if not output.success or not isinstance(output.json_data, dict):
        raise RuntimeError("Unable to validate model definitions from the regional model catalog.")

    catalog = output.json_data.get("value", [])
    for requested_model in models_config:
        model_name = requested_model.get("name")
        model_version = requested_model.get("version")
        model_sku = requested_model.get("sku")
        requested_capacity = requested_model.get("capacity", 0)
        matches = [
            entry.get("model", {})
            for entry in catalog
            if entry.get("model", {}).get("format") == requested_model.get("publisher", requested_model.get("format"))
            and entry.get("model", {}).get("name") == model_name
            and entry.get("model", {}).get("version") == model_version
        ]

        if not matches:
            raise RuntimeError(
                f"Model '{model_name}' version '{model_version}' is not available in '{location}'."
            )

        active_matches = [
            model for model in matches
            if model.get("lifecycleStatus") == "GenerallyAvailable"
        ]
        if not active_matches:
            status = matches[0].get("lifecycleStatus", "unknown")
            raise RuntimeError(
                f"Model '{model_name}' version '{model_version}' is not deployable in '{location}'; "
                f"its lifecycle status is '{status}'."
            )

        sku_matches = [
            sku for model in active_matches for sku in model.get("skus", [])
            if sku.get("name") == model_sku
        ]
        if not sku_matches:
            raise RuntimeError(
                f"Model '{model_name}' version '{model_version}' does not support SKU '{model_sku}' in '{location}'."
            )

        maximum_capacity = sku_matches[0].get("capacity", {}).get("maximum")
        if maximum_capacity is not None and requested_capacity > maximum_capacity:
            raise RuntimeError(
                f"Model '{model_name}' SKU '{model_sku}' supports a maximum capacity of "
                f"{maximum_capacity}, but {requested_capacity} was requested."
            )

    print_ok(f"Validated {len(models_config)} model definition(s) in '{location}'")
    return True

# Retrieves resources in a resource group
def get_resources(resource_group_name, config):
    if not resource_group_name:
        print_error("Missing resource group name parameter.")
        return

    resources = {}
    try:
        ## retrieve resource group location
        output = run(f"az group show --name {resource_group_name}")

        if output.success:
            print_info(f"Using existing resource group '{resource_group_name}'")
            output = run(f"az group show --name {resource_group_name} -o json", "Retrieved resource group ", "Failed to retrieve resource group")
            if output.success and output.json_data:
                resources['resourceGroupLocation'] = output.json_data["location"]

                ## retrieve resources
                output = run(f'az resource list -g {resource_group_name} -o json', "Listed resources", "Failed to list resources")
                if output.success and output.json_data:
                    for resource in output.json_data:
                        match resource["type"].lower():
                            case "microsoft.operationalinsights/workspaces":
                                resources['logAnalyticsResourceId'] = resource["id"]
                                resources['logAnalyticsResourceName'] = resource["name"]
                            case "microsoft.insights/components":
                                resources['appInsightsResourceId'] = resource["id"]
                                resources['appInsightsResourceName'] = resource["name"]
                                output = run(f'az resource show -g {resource_group_name} -n {resource["name"]} --resource-type "microsoft.insights/components" -o json', "Retrieved App Insights resource", "Failed to retrieve App Insights resource")
                                if output.success and output.json_data:
                                    resources['appInsightsInstrumentationKey'] = output.json_data["properties"]["InstrumentationKey"]
                            case "microsoft.cognitiveservices/accounts":
                                resources['foundryResourceId'] = resource["id"]
                                resources['foundryResourceName'] = resource["name"]
                            case "microsoft.cognitiveservices/accounts/projects":
                                resources['foundryProjectId'] = resource["id"]
                                resources['foundryProjectName'] = resource["name"]
                            case "microsoft.apimanagement/service":
                                resources['apimResourceId'] = resource["id"]
                                resources['apimResourceName'] = resource["name"]
                                resources['apimPrincipalId'] = resource["identity"]["principalId"]
        else:
            return config

    except Exception as e:
        print_error(f"Error retrieving resources: {e}")

    return resources

# Cleans up resources associated with a deployment in a resource group
def cleanup_resources(deployment_name, resource_group_name = None):
    if not deployment_name:
        print_error("Missing deployment name parameter.")
        return

    if not resource_group_name:
        resource_group_name = f"lab-{deployment_name}"

    try:
        print_info(f"🧹 Cleaning up resource group '{resource_group_name}'...")

        # Show the deployment details
        output = run(f"az deployment group show --name {deployment_name} -g {resource_group_name} -o json", "Deployment retrieved", "Failed to retrieve the deployment")

        if output.success and output.json_data:
            provisioning_state = output.json_data.get("properties").get("provisioningState")
            print_info(f"Deployment provisioning state: {provisioning_state}")

            # Delete AI Foundry projects
            output = run(f'az resource list -g {resource_group_name} --resource-type "microsoft.cognitiveservices/accounts/projects"', "Retrieved AI Foundry projects", "Failed to list AI Foundry projects")
            if output.success and output.json_data:
                for resource in output.json_data:
                    print_info(f"Deleting AI Foundry project '{resource['name']}' in resource group '{resource_group_name}'...")
                    output = run(f'az resource delete --ids "{resource['id']}"', f"AI Foundry project '{resource['name']}' deleted", f"Failed to delete AI Foundry project '{resource['name']}'")

            # Delete and purge CognitiveService accounts
            output = run(f"az cognitiveservices account list -g {resource_group_name}", f"Listed CognitiveService accounts", f"Failed to list CognitiveService accounts")
            if output.success and output.json_data:
                for resource in output.json_data:
                    print_info(f"Deleting and purging Cognitive Service Account '{resource['name']}' in resource group '{resource_group_name}'...")
                    output = run(f"az cognitiveservices account delete -g {resource_group_name} -n {resource['name']}", f"Cognitive Services '{resource['name']}' deleted", f"Failed to delete Cognitive Services '{resource['name']}'")
                    output = run(f"az cognitiveservices account purge -g {resource_group_name} -n {resource['name']} -l \"{resource['location']}\"", f"Cognitive Services '{resource['name']}' purged", f"Failed to purge Cognitive Services '{resource['name']}'")

            # Delete and purge APIM resources
            output = run(f" az apim list -g {resource_group_name}", f"Listed APIM resources", f"Failed to list APIM resources")
            if output.success and output.json_data:
                for resource in output.json_data:
                    print_info(f"Deleting and purging API Management '{resource['name']}' in resource group '{resource_group_name}'...")
                    output = run(f"az apim delete -n {resource['name']} -g {resource_group_name} -y", f"API Management '{resource['name']}' deleted", f"Failed to delete API Management '{resource['name']}'")
                    output = run(f"az apim deletedservice purge --service-name {resource['name']} --location \"{resource['location']}\"", f"API Management '{resource['name']}' purged", f"Failed to purge API Management '{resource['name']}'")

            # Delete and purge Key Vault resources
            output = run(f"az keyvault list -g {resource_group_name}", f"Listed Key Vault resources", f"Failed to list Key Vault resources")
            if output.success and output.json_data:
                for resource in output.json_data:
                    print_info(f"Deleting and purging Key Vault '{resource['name']}' in resource group '{resource_group_name}'...")
                    output = run(f"az keyvault delete -n {resource['name']} -g {resource_group_name}", f"Key Vault '{resource['name']}' deleted", f"Failed to delete Key Vault '{resource['name']}'")
                    output = run(f"az keyvault purge -n {resource['name']} --location \"{resource['location']}\"", f"Key Vault '{resource['name']}' purged", f"Failed to purge Key Vault '{resource['name']}'")

            # Delete the resource group last
            print_message(f"🧹 Deleting resource group '{resource_group_name}'...")
            output = run(f"az group delete --name {resource_group_name} -y", f"Resource group '{resource_group_name}' deleted", f"Failed to delete resource group '{resource_group_name}'")

            print_message("🧹 Cleanup completed.")

    except Exception as e:
        print(f"An error occurred during cleanup: {e}")
        traceback.print_exc()

def create_resource_group(resource_group_name, resource_group_location = None):
    if not resource_group_name:
        print_error('Please specify the resource group name.')
    else:
        output = run(f"az group show --name {resource_group_name}")

        if output.success:
            print_info(f"Using existing resource group '{resource_group_name}'")
        else:
            if not resource_group_location:
                print_error('Please specify the resource group location.')
            else:
                print_info(f"Resource group {resource_group_name} does not yet exist. Creating the resource group now...")

                output = run(f"az group create --name {resource_group_name} --location {resource_group_location} --tags source=ai-gateway",
                    f"Resource group '{resource_group_name}' created",
                    f"Failed to create the resource group '{resource_group_name}'")

# Deletes a specific resource based on its type
def delete_resource(resource, resource_group_name):
    resource_name = resource.get("name")
    resource_type = resource.get("type")
    resource_location = resource.get("location")

    print(f"🗑 Deleting {resource_type} '{resource_name}' in resource group '{resource_group_name}'...")

    # API Management
    if resource_type == "Microsoft.ApiManagement/service":
        output = run(f"az apim delete -n {resource_name} -g {resource_group_name} -y", f"API Management '{resource_name}' deleted", f"Failed to delete API Management '{resource_name}'")

        output = run(f"az apim deletedservice purge --service-name {resource_name} --location \"{resource_location}\"", f"API Management '{resource_name}' purged", f"Failed to purge API Management '{resource_name}'")

    # Cognitive Services
    elif resource_type == "Microsoft.CognitiveServices/accounts":
        output = run(f"az cognitiveservices account delete -g {resource_group_name} -n {resource_name}", f"Cognitive Services '{resource_name}' deleted", f"Failed to delete Cognitive Services '{resource_name}'")

        output = run(f"az cognitiveservices account purge -g {resource_group_name} -n {resource_name} -l \"{resource_location}\"", f"Cognitive Services '{resource_name}' purged", f"Failed to purge Cognitive Services '{resource_name}'")

    # Key Vault
    elif resource_type == "Microsoft.KeyVault/vaults":
        output = run(f"az keyvault delete -n {resource_name} -g {resource_group_name}", f"Key Vault '{resource_name}' deleted", f"Failed to delete Key Vault '{resource_name}'")

def get_deployment_output(output, output_property, output_label = '', secure = False) -> str:
    try:
        deployment_output = output.json_data['properties']['outputs'][output_property]['value']

        if output_label:
            if secure:
                print_info(f"{output_label}: ****{deployment_output[-4:]}")
            else:
                print_info(f"{output_label}: {deployment_output}")

        return str(deployment_output)
    except Exception as e:
        error = f"Failed to retrieve output property: '{output_property}'\nError: {e}"
        print_error(error)
        raise Exception(error)

def print_response(response):
    print("Response headers: ", response.headers)

    if (response.status_code == 200):
        print_ok(f"Status Code: {response.status_code}")
        data = json.loads(response.text)
        print(json.dumps(data, indent=4))
    else:
        print_warning(f"Status Code: {response.status_code}")
        print(response.text)

def print_response_code(response):
    # Check the response status code and apply formatting
    if 200 <= response.status_code < 300:
        status_code_str = f"{BOLD_GREEN}{response.status_code} - {response.reason}{RESET_FORMATTING}"
    elif response.status_code >= 400:
        status_code_str = f"{BOLD_RED}{response.status_code} - {response.reason}{RESET_FORMATTING}"
    else:
        status_code_str = str(response.status_code)

    # Print the response status with the appropriate formatting
    print(f"Response status: {status_code_str}")

# Simple: print full error body (JSON if available, else raw text)
def print_full_http_error(response):
    try:
        data = response.json()
        print_error("Request failed. Full JSON body:", json.dumps(data, indent=2))
        # If ARM-style error present, surface message too
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code", "")
            msg = data["error"].get("message", "")
            if msg or code:
                print_error(f"Service error:", f"{code} - {msg}")
    except ValueError:
        print_error("Request failed. Full text body:", response.text or "")

def run(command, ok_message = '', error_message = '', print_output = False, print_command_to_run = True):
    if print_command_to_run:
        print_command(command)

    start_time = time.time()

    try:
        completed_process = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output_text = completed_process.stdout
        success = completed_process.returncode == 0
    except subprocess.CalledProcessError as e:
        output_text = e.output.decode("utf-8")
        success = False

    minutes, seconds = divmod(time.time() - start_time, 60)

    print_message = print_ok if success else print_error

    if (ok_message or error_message):
        print_message(ok_message if success else error_message, output_text if not success or print_output  else "", f"[{int(minutes)}m:{int(seconds)}s]")

    return Output(success, output_text)

def create_bicep_params(policy_xml_filepath, parameters_filepath, bicep_parameters, replacements_list):
    # Read the specified policy XML file
    with open(policy_xml_filepath, 'r') as policy_xml_file:
        policy_template_xml = policy_xml_file.read()

    # Replace the placeholders in the policy XML with the actual values from the replacements_lists array
    for key, value in replacements_list:
        policy_template_xml = policy_template_xml.replace(key, str(value))

    # Set or update the policyXml parameter in the bicep parameters file
    bicep_parameters['parameters'].setdefault('policyXml', {})
    bicep_parameters['parameters']['policyXml']['value'] = policy_template_xml

    # Write the updated bicep parameters to the specified parameters file
    with open(parameters_filepath, 'w') as bicep_parameters_file:
        bicep_parameters_file.write(json.dumps(bicep_parameters))

    print(f"📝 Updated the policy XML in the bicep parameters file '{parameters_filepath}'")

    return bicep_parameters

def update_api_policy(subscription_id, resource_group_name, apim_service_name, api_id, policy_xml):
    # We first need to obtain an access token for the REST API
    output = run(f"az account get-access-token --resource https://management.azure.com/",
        f"Successfully obtained access token", f"Failed to obtain access token")

    if output.success and output.json_data:
        access_token = output.json_data['accessToken']

        print("Updating the API policy...")
        # https://learn.microsoft.com/en-us/rest/api/apimanagement/api-policy/create-or-update?view=rest-apimanagement-2024-06-01-preview
        url = f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group_name}/providers/Microsoft.ApiManagement/service/{apim_service_name}/apis/{api_id}/policies/policy?api-version=2024-06-01-preview"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        body = {
            "properties": {
                "format": "rawxml",
                "value": policy_xml
            }
        }

        response = requests.put(url, headers = headers, json = body)
        if 200 <= response.status_code < 300:
            print_response_code(response)
        else:
            print_response_code(response)
            print_full_http_error(response)

def update_api_operation_policy(subscription_id, resource_group_name, apim_service_name, api_id, operation_id, policy_xml):
    # We first need to obtain an access token for the REST API
    output = run(f"az account get-access-token --resource https://management.azure.com/",
        f"Successfully obtained access token", f"Failed to obtain access token")

    if output.success and output.json_data:
        access_token = output.json_data['accessToken']

        print("Updating the API policy...")
        # https://learn.microsoft.com/en-us/rest/api/apimanagement/api-policy/create-or-update?view=rest-apimanagement-2024-06-01-preview
        url = f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group_name}/providers/Microsoft.ApiManagement/service/{apim_service_name}/apis/{api_id}/operations/{operation_id}/policies/policy?api-version=2024-06-01-preview"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        body = {
            "properties": {
                "format": "rawxml",
                "value": policy_xml
            }
        }

        response = requests.put(url, headers = headers, json = body)
        print_response_code(response)

def get_debug_credentials(apim_service_id, api_id, expire_after = 'PT1H') -> str | None:
    request = {
        "credentialsExpireAfter": expire_after,
        "apiId": f"{apim_service_id}/apis/{api_id}",
        "purposes": ["tracing"]
    }
    output = run(f"az rest --method post --uri {apim_service_id}/gateways/managed/listDebugCredentials?api-version=2023-05-01-preview --body \"{str(request)}\"",
            "Retrieved APIM debug credentials", "Failed to get the APIM debug credentials")
    return output.json_data['token'] if output.success and output.json_data else None
        
def get_trace(apim_service_id, trace_id) -> str | None:
    request = {
        "traceId": trace_id
    }
    output = run(f"az rest --method post --uri {apim_service_id}/gateways/managed/listTrace?api-version=2023-05-01-preview --body \"{str(request)}\"",
            "Retrieved trace details", "Failed to get the trace details")
    return output.json_data if output.success and output.json_data else None

