import os

os.environ["OMP_NUM_THREADS"] = "1"

from collections import Counter
from utils.text_encoder import TokenTextEncoder

from utils.multiprocess_utils import chunked_multiprocess_run
import random
import traceback
import json
from resemblyzer import VoiceEncoder
from tqdm import tqdm
from data_gen.tts.data_gen_utils import get_mel2ph, get_pitch, build_phone_encoder, is_sil_phoneme
from utils.hparams import hparams, set_hparams
import numpy as np
from utils.indexed_datasets import IndexedDatasetBuilder
from vocoders.base_vocoder import get_vocoder_cls
import pandas as pd


class BinarizationError(Exception):
    pass


class VocoderBinarizer:
    def __init__(self, processed_data_dir=None):
        if processed_data_dir is None:
            processed_data_dir = hparams['processed_data_dir']
        self.processed_data_dirs = processed_data_dir.split(",")
        self.binarization_args = hparams['binarization_args']
        self.pre_align_args = hparams['pre_align_args']
        # self.item2txt = {}
        # self.item2ph = {}
        self.item2wavfn = {}

    def load_meta_data(self):
        for ds_id, processed_data_dir in enumerate(self.processed_data_dirs):
            self.meta_df = pd.read_csv(f"{processed_data_dir}/metadata_phone.csv", dtype=str)
            for r_idx, r in tqdm(self.meta_df.iterrows(), desc='Loading meta data.'):
                item_name = raw_item_name = r['item_name']
                if len(self.processed_data_dirs) > 1:
                    item_name = f'ds{ds_id}_{item_name}'
                self.item2wavfn[item_name] = r['wav_fn']
                # self.item2tgfn[item_name] = f"{processed_data_dir}/mfa_outputs/{raw_item_name}.TextGrid"
        self.item_names = sorted(list(self.item2wavfn.keys()))
        if self.binarization_args['shuffle']:
            random.seed(1234)
            random.shuffle(self.item_names)

    @property
    def train_item_names(self):
        return self.item_names[hparams['test_num']:]

    @property
    def valid_item_names(self):
        return self.item_names[:hparams['test_num']]

    @property
    def test_item_names(self):
        return self.valid_item_names

    def build_spk_map(self):
        spk_map = set()
        for item_name in self.item_names:
            spk_name = self.item2spk[item_name]
            spk_map.add(spk_name)
        spk_map = {x: i for i, x in enumerate(sorted(list(spk_map)))}
        print("| #Spk: ", len(spk_map))
        assert len(spk_map) == 0 or len(spk_map) <= hparams['num_spk'], len(spk_map)
        return spk_map

    def item_name2spk_id(self, item_name):
        return self.spk_map[self.item2spk[item_name]]

    def _phone_encoder(self):
        ph_set_fn = f"{hparams['binary_data_dir']}/phone_set.json"
        ph_set = []
        if self.binarization_args['reset_phone_dict'] or not os.path.exists(ph_set_fn):
            for ph_sent in self.item2ph.values():
                ph_set += ph_sent.split(' ')
            ph_set = sorted(set(ph_set))
            json.dump(ph_set, open(ph_set_fn, 'w'))
            print("| Build phone set: ", ph_set)
        else:
            ph_set = json.load(open(ph_set_fn, 'r'))
            print("| Load phone set: ", ph_set)
        return build_phone_encoder(hparams['binary_data_dir'])

    def _word_encoder(self):
        fn = f"{hparams['binary_data_dir']}/word_set.json"
        word_set = []
        if self.binarization_args['reset_word_dict']:
            for word_sent in self.item2txt.values():
                word_set += [x for x in word_sent.split(' ') if x != '']
            word_set = Counter(word_set)
            total_words = sum(word_set.values())
            word_set = word_set.most_common(hparams['word_size'])
            num_unk_words = total_words - sum([x[1] for x in word_set])
            word_set = [x[0] for x in word_set]
            json.dump(word_set, open(fn, 'w'))
            print(f"| Build word set. Size: {len(word_set)}, #total words: {total_words},"
                  f" #unk_words: {num_unk_words}, word_set[:10]:, {word_set[:10]}.")
        else:
            word_set = json.load(open(fn, 'r'))
            print("| Load word set. Size: ", len(word_set), word_set[:10])
        return TokenTextEncoder(None, vocab_list=word_set, replace_oov='<UNK>')

    def meta_data(self, prefix):
        if prefix == 'valid':
            item_names = self.valid_item_names
        elif prefix == 'test':
            item_names = self.test_item_names
        else:
            item_names = self.train_item_names
        for item_name in item_names:
            # ph = self.item2ph[item_name]
            # txt = self.item2txt[item_name]
            wav_fn = self.item2wavfn[item_name]
            yield item_name, wav_fn

    def process(self):
        self.load_meta_data()
        os.makedirs(hparams['binary_data_dir'], exist_ok=True)
        # self.spk_map = self.build_spk_map()
        # print("| spk_map: ", self.spk_map)
        # spk_map_fn = f"{hparams['binary_data_dir']}/spk_map.json"
        # json.dump(self.spk_map, open(spk_map_fn, 'w'))

        # self.phone_encoder = self._phone_encoder()
        # self.word_encoder = None
        # if self.binarization_args['with_word']:
        #     self.word_encoder = self._word_encoder()
        self.process_data('valid')
        self.process_data('test')
        self.process_data('train')

    def process_data(self, prefix):
        data_dir = hparams['binary_data_dir']
        args = []
        builder = IndexedDatasetBuilder(f'{data_dir}/{prefix}')
        mel_lengths = []
        total_sec = 0
        meta_data = list(self.meta_data(prefix))
        for m in meta_data:
            args.append(list(m) + [self.binarization_args])
        num_workers = self.num_workers
        for f_id, (_, item) in enumerate(
                zip(tqdm(meta_data), chunked_multiprocess_run(self.process_item, args, num_workers=num_workers))):
            if item is None:
                continue
            if not self.binarization_args['with_wav'] and 'wav' in item:
                del item['wav']
            builder.add_item(item)
            mel_lengths.append(item['len'])
            total_sec += item['sec']
        builder.finalize()
        np.save(f'{data_dir}/{prefix}_lengths.npy', mel_lengths)
        print(f"| {prefix} total duration: {total_sec:.3f}s")

    @classmethod
    def process_item(cls, item_name, wav_fn, binarization_args):
        res = {'item_name': item_name, 'wav_fn': wav_fn}
        if binarization_args['with_linear']:
            wav, mel, linear_stft = get_vocoder_cls(hparams).wav2spec(wav_fn, return_linear=True)
            res['linear'] = linear_stft
        else:
            wav, mel = get_vocoder_cls(hparams).wav2spec(wav_fn)
        wav = wav.astype(np.float16)
        res.update({'mel': mel, 'wav': wav,
                    'sec': len(wav) / hparams['audio_sample_rate'], 'len': mel.shape[0]})
        
        return res

    @staticmethod
    def get_align(tg_fn, res):
        ph = res['ph']
        mel = res['mel']
        phone_encoded = res['phone']
        if tg_fn is not None and os.path.exists(tg_fn):
            mel2ph, dur = get_mel2ph(tg_fn, ph, mel, hparams)
        else:
            raise BinarizationError(f"Align not found")
        if mel2ph.max() - 1 >= len(phone_encoded):
            raise BinarizationError(
                f"Align does not match: mel2ph.max() - 1: {mel2ph.max() - 1}, len(phone_encoded): {len(phone_encoded)}")
        res['mel2ph'] = mel2ph
        res['dur'] = dur

    @staticmethod
    def get_pitch(res):
        wav, mel = res['wav'], res['mel']
        f0, pitch_coarse = get_pitch(wav, mel, hparams)
        if sum(f0) == 0:
            raise BinarizationError("Empty f0")
        res['f0'] = f0
        res['pitch'] = pitch_coarse

    @staticmethod
    def get_f0cwt(res):
        from utils.cwt import get_cont_lf0, get_lf0_cwt
        f0 = res['f0']
        uv, cont_lf0_lpf = get_cont_lf0(f0)
        logf0s_mean_org, logf0s_std_org = np.mean(cont_lf0_lpf), np.std(cont_lf0_lpf)
        cont_lf0_lpf_norm = (cont_lf0_lpf - logf0s_mean_org) / logf0s_std_org
        Wavelet_lf0, scales = get_lf0_cwt(cont_lf0_lpf_norm)
        if np.any(np.isnan(Wavelet_lf0)):
            raise BinarizationError("NaN CWT")
        res['cwt_spec'] = Wavelet_lf0
        res['cwt_scales'] = scales
        res['f0_mean'] = logf0s_mean_org
        res['f0_std'] = logf0s_std_org

    @staticmethod
    def get_word(res, word_encoder):
        ph_split = res['ph'].split(" ")
        # ph side mapping to word
        ph_words = []  # ['<BOS>', 'N_AW1_', ',', 'AE1_Z_|', 'AO1_L_|', 'B_UH1_K_S_|', 'N_AA1_T_|', ....]
        ph2word = np.zeros([len(ph_split)], dtype=int)
        last_ph_idx_for_word = []  # [2, 11, ...]
        for i, ph in enumerate(ph_split):
            if ph == '|':
                last_ph_idx_for_word.append(i)
            elif not ph[0].isalnum():
                if ph not in ['<BOS>']:
                    last_ph_idx_for_word.append(i - 1)
                last_ph_idx_for_word.append(i)
        start_ph_idx_for_word = [0] + [i + 1 for i in last_ph_idx_for_word[:-1]]
        for i, (s_w, e_w) in enumerate(zip(start_ph_idx_for_word, last_ph_idx_for_word)):
            ph_words.append(ph_split[s_w:e_w + 1])
            ph2word[s_w:e_w + 1] = i
        ph2word = ph2word.tolist()
        ph_words = ["_".join(w) for w in ph_words]

        # mel side mapping to word
        mel2word = []
        dur_word = [0 for _ in range(len(ph_words))]
        for i, m2p in enumerate(res['mel2ph']):
            word_idx = ph2word[m2p - 1]
            mel2word.append(ph2word[m2p - 1])
            dur_word[word_idx] += 1
        ph2word = [x + 1 for x in ph2word]  # 0预留给padding
        mel2word = [x + 1 for x in mel2word]  # 0预留给padding
        res['ph_words'] = ph_words  # [T_word]
        res['ph2word'] = ph2word  # [T_ph]
        res['mel2word'] = mel2word  # [T_mel]
        res['dur_word'] = dur_word  # [T_word]
        words = [x for x in res['txt'].split(" ") if x != '']
        while len(words) > 0 and is_sil_phoneme(words[0]):
            words = words[1:]
        while len(words) > 0 and is_sil_phoneme(words[-1]):
            words = words[:-1]
        words = ['<BOS>'] + words + ['<EOS>']
        word_tokens = word_encoder.encode(" ".join(words))
        res['words'] = words
        res['word_tokens'] = word_tokens
        assert len(words) == len(ph_words), [words, ph_words]

    @classmethod
    def process_mel_item(cls, item_name, mel, wav_fn, binarization_args):
        res = {'item_name': item_name, 'wav_fn': wav_fn}
        mel = mel
        wav = np.ones((1,500,100))
        res.update({'mel': mel, 'wav': wav,
                    'sec': 0, 'len': mel.shape[0]})
        return res

    @property
    def num_workers(self):
        return int(os.getenv('N_PROC', hparams.get('N_PROC', os.cpu_count())))


if __name__ == "__main__":
    set_hparams()
    VocoderBinarizer().process()
