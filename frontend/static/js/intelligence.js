import { api, showError } from "./api.js";
import { capabilityList } from "./capabilities.js";

const WANTED = ["tesseract", "ocrmypdf", "docling", "ollama", "chromadb"];
api("/api/system/capabilities").then((data) => {
  const items = data.integrations.filter((item) => WANTED.includes(item.id));
  document.getElementById("engines").replaceChildren(...capabilityList(items));
}).catch(showError);
