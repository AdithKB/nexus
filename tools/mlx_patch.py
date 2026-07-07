from mlx_lm.utils import hf_repo_to_path

def get_model_path(model_id_or_path, revision=None):
    # Ignore the revision argument to match the new API signature
    return hf_repo_to_path(model_id_or_path)
