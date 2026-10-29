'''RTM asset application and undo follow the native block processor lifecycle.'''

from electrumx.server.block_processor import BlockProcessor


class RaptoreumBlockProcessor(BlockProcessor):
    def advance_txs(self, txs, is_unspendable):
        height = len(self.db.tx_counts)
        keep_undo = height >= self.db.min_undo_height(self.daemon.cached_height())
        touched = self.db.assets.apply_block(txs, height, self.tx_count, keep_undo)
        self.touched.update(touched)
        return super().advance_txs(txs, is_unspendable)

    def backup_txs(self, txs, is_unspendable):
        self.db.assets.rollback(self.height)
        return super().backup_txs(txs, is_unspendable)

    def check_cache_size(self):
        if self.db.assets.memory_size() >= max(1, self.env.cache_MB) * 250000:
            return True
        return super().check_cache_size()
