import os  
import sys
sys.path.append(os.getcwd())
import torch   
import hydra  
import lightning.pytorch as pl
from modelzipper.tutils import *
from utils import get_model_tokenizer, CustomDatamodule
from evaluate.evaluator import Evaluator
from dev_configs.config import parse_args, get_final_configs

class Experiment(pl.LightningModule):
    def __init__(self, model, config, tokenizer=None, state="eval") -> None:
        super(Experiment, self).__init__()
        self.model = model
        self.model.eval()
        self.cfg = config
        self.tokenizer = tokenizer
        if "inference_cfg" in config.task:
        # if hasattr(config.task, "inference_cfg"):  # what to save for task setting
            for key in config.task.inference_cfg:
                if isinstance(key, int):
                    key = str(key)
                setattr(self, key, config.task.inference_cfg[key])
        try:
            self.hold_graph = self.params['retain_first_backpass']
        except:
            pass

    @torch.no_grad()
    def predict_step(self, batch, batch_idx, dataloader_idx=None):

        input_ids = batch.pop("input_ids")
        # import pdb;pdb.set_trace()
        if "ar" in self.cfg.task.dataset.data_name.lower():
            output = self.model(input_ids).logits.max(-1)[1]
            final_res = {}
            final_res['predictions'] = output[0]
            final_res['labels'] = batch.pop('label')
        elif "longbench" in self.cfg.task.dataset.data_name.lower():
            max_gen_len = batch.pop("max_generation_len")
            context_length = input_ids.shape[-1]
            if self.cfg.task.dataset.subtask == "samsum": 
                output = self.model.generate(
                    input_ids,
                    max_length=int(input_ids.size(-1) + max_gen_len),
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                    min_length=context_length+1,
                    eos_token_id=[self.tokenizer.eos_token_id, self.tokenizer.encode("\n", add_special_tokens=False)[-1]],
                )[0]
            else:
                output = self.model.generate(
                    input_ids,
                    max_length=int(input_ids.size(-1) + max_gen_len),
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                )[0]
            
            pred = self.tokenizer.decode(output[context_length:], skip_special_tokens=True)

            # import pdb;pdb.set_trace()
            final_res = {}
            # final_res['predictions'] = output[0]
            final_res['answers'] = pred
            final_res['labels'] = batch.pop('answers')

        else:
            
            output = self.model.generate(
                input_ids, 
                max_length=input_ids.size(-1) + self.cfg.task.other_cfgs.max_generation_length,
                min_length=input_ids.size(-1) + 10, 
                eos_token_id=self.tokenizer.eos_token_id, 
            )
            final_res = {}
            final_res['predictions'] = output[0]

        return final_res
        

def main(config):

    model_root_dir = config.platform.hf_model_path
    save_root_dir = config.platform.result_path
    data_root_dir = config.platform.dataset_path

    # if use_custom_module 
    use_custom_module = False
    if hasattr(config.model, "use_custom_module"):
        use_custom_module = config.model.use_custom_module

    model, tokenizer = get_model_tokenizer(
        model_root_dir, 
        config.model, 
        use_custom_module=use_custom_module,
    )

    # load data module
    data_module = CustomDatamodule(config.task, data_root_dir, tokenizer)
    data_module.setup(stage='predict')

    # load experiment (and model checkpoint)
    experiment = Experiment(model=model, config=config, tokenizer=tokenizer)

    # init tester
    tester = pl.Trainer(devices=config.experiment.device_num)

    # predict the results
    predictions = tester.predict(
        model=experiment,
        dataloaders=data_module.predict_dataloader(),
        return_predictions=True,
    )



    # load testing data
    if "longbench"  in config.task.dataset.data_name.lower():
        # subtask = [["qasper", "multifieldqa_en", "hotpotqa"], ["2wikimqa", "gov_report", "multi_news"], \
        #             ["musique", "trec", "triviaqa", "samsum"], ["passage_count", "passage_retrieval_en", "qmsum","narrativeqa"]]
        # subtask = [["qasper"]]
        subtask = [["narrativeqa", "qasper", "multifieldqa_en", "hotpotqa", "2wikimqa", "musique", "gov_report",  "qmsum" ,\
                    "multi_news", "trec", "triviaqa", "samsum", "passage_count", "passage_retrieval_en"]]
        if config.task.subtask == "None":
            subtask = subtask[0]    
        elif isinstance(config.task.dataset.subtask, list):
            subtask = config.task.dataset.subtask
    else:
        subtask =  [config.task.dataset.data_name.lower()]
        
    for task in subtask:
        data_module = CustomDatamodule(config.task, data_root_dir, tokenizer)
        data_module.setup(stage='predict')

        # load experiment (and model checkpoint)
        experiment = Experiment(model=model, config=config, tokenizer=tokenizer)
        
        tester = pl.Trainer(devices=config.experiment.device_num)

        b_t = time.time()
        # import pdb;pdb.set_trace()
      
        predictions = tester.predict(
            model=experiment,
            dataloaders=data_module.predict_dataloader(),
            return_predictions=True,
        )

        print_c(f"======= prediction end, begin to post process and save =======", "magenta")
        # if task == config.exp_task: 
        #     save_path = os.path.join(save_root_dir, f"{config.experiment.results_save_dir}/")
        #     save_path=os.path.dirname(os.path.dirname(os.path.dirname(save_path)))
        #     save_final_path = os.path.join(save_root_dir, f"{config.experiment.results_save_dir}/predictions.pkl")
        # else:
        cur_task = args.data_name
        save_path = os.path.join(save_root_dir, f"{config.task.dataset.data_name}/",f"{args.model_name_or_path}/",f"{config.experiment.experiment_name}/")
        # save_path = os.path.join(save_root_dir, f"{config.experiment.experiment_name}/")
        
        save_final_path = save_path + str(task)+ "_predictions.pkl"
        auto_save_data(predictions, save_final_path)
        print_c(f"save predictions to {save_final_path}, total cost time: {time.time() - b_t}", "magenta")

        eval = Evaluator(
            root_dir=save_root_dir, fpath=save_final_path, 
            data_path=data_root_dir,
            task=cur_task,
            subtask=config.experiment.experiment_name,
            tokenizer_name_or_path=None,
            value=None, save_evaluation_path=os.path.dirname(os.path.dirname(save_path)),
            save_gen_res=True,
        )

    
   

if __name__ == '__main__':

    args = parse_args()
    config = get_final_configs(args)
    print_c(config, 'yellow')

    main(config)
